"""Talk to the installed Antigravity app through its own language server.

Why this file exists: Antigravity is not a CLI. The only native surface is the
Electron app, which owns a Go language server (`resources/bin/language_server.exe`)
and talks to it over Connect (gRPC/JSON) on a loopback port. Driving the model
through a bare API would skip the app's own tools, permissions and workspace
handling, so everything here goes through that language server instead.

The request shapes below are not guesses. They were read from the shipped
renderer bundle (`main.js`, 11.7 MB) served by the running app, and confirmed by
a real turn that produced a `CORTEX_STEP_TYPE_CODE_ACTION` with
`toolCall.name == "write_file"` and a file on disk. Two details matter:

* `workspaceUris` must be `file://` URIs. A bare `C:/...` path is rejected with
  `unknown uri scheme`, before any model is consulted.
* The model is `cascadeConfig.plannerConfig.planModel`, an enum name such as
  `MODEL_PLACEHOLDER_M318`. It is a *session* setting: putting it one level up
  (`cascadeConfig.planModel`) is silently ignored, and leaving it out fails with
  `plan model not specified`. It must therefore come from the app's own model
  catalogue, never from a constant baked into this package.
"""

import json
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .domain import Invalid

SERVICE = "exa.language_server_pb.LanguageServerService"
CONNECT_VERSION = "1"

# Step types the relay reports. Anything else is carried as its raw name so an
# unknown step is visible rather than silently dropped.
TOOL_STEP = "CORTEX_STEP_TYPE_CODE_ACTION"
ASSISTANT_STEP = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
USER_STEP = "CORTEX_STEP_TYPE_USER_INPUT"
ERROR_STEP = "CORTEX_STEP_TYPE_ERROR_MESSAGE"
MCP_STEP = "CORTEX_STEP_TYPE_MCP_TOOL"


def workspace_uri(path):
    """The language server only accepts `file://` workspace URIs."""
    resolved = Path(path).resolve()
    return resolved.as_uri()


class AntigravityServer:
    """A loopback Connect client for one running Antigravity instance.

    `csrf_token` is required on every call; the app refuses requests without it.
    The token is not stored here — the caller passes it per instance, and nothing
    in this module writes it to disk.
    """

    def __init__(self, port, csrf_token, secure=True, timeout=30):
        self.port = int(port)
        self.csrf_token = csrf_token
        self.secure = secure
        self.timeout = timeout
        self.base = f"{'https' if secure else 'http'}://127.0.0.1:{self.port}"
        self._context = None
        if secure:
            # The app serves a self-signed loopback certificate.
            self._context = ssl.create_default_context()
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

    def call(self, method, payload=None):
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}/{SERVICE}/{method}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": CONNECT_VERSION,
                "X-Codeium-Csrf-Token": self.csrf_token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._context) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:400]
            raise Invalid(f"Antigravity 调用 {method} 失败（HTTP {error.code}）：{detail}") from None
        except (urllib.error.URLError, OSError) as error:
            raise Invalid(f"连不上 Antigravity 语言服务器（{self.base}）：{error}") from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise Invalid(f"Antigravity 返回了非 JSON 响应（{method}）。") from None

    # --- discovery -----------------------------------------------------

    def heartbeat(self):
        return self.call("Heartbeat")

    def models(self):
        """The app's own catalogue; `planModel` must be one of these names."""
        data = self.call("GetCascadeModelConfigData")
        entries = []
        for item in data.get("clientModelConfigs") or []:
            name = (item.get("modelOrAlias") or {}).get("model")
            if name:
                entries.append({"model": name, "label": item.get("label"), "id": item.get("modelId")})
        return entries

    def trajectories(self):
        return self.call("GetAllCascadeTrajectories")

    # --- turns ---------------------------------------------------------

    @staticmethod
    def config(plan_model):
        """The nesting the app itself builds; one level up is ignored."""
        if not plan_model:
            raise Invalid("缺少会话级模型名，无法构造 Antigravity 请求。")
        return {"plannerConfig": {"planModel": plan_model}}

    def start(self, workspace, plan_model):
        payload = {
            "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
            "workspaceUris": [workspace_uri(workspace)],
            "cascadeConfig": self.config(plan_model),
        }
        data = self.call("StartCascade", payload)
        cascade = data.get("cascadeId")
        if not cascade:
            raise Invalid("Antigravity 没有返回 cascadeId。")
        return cascade

    def send(self, cascade, text, plan_model):
        return self.call("SendUserCascadeMessage", {
            "cascadeId": cascade,
            "cascadeConfig": self.config(plan_model),
            "text": text,
        })

    def steps(self, cascade):
        return self.call("GetCascadeTrajectorySteps", {"cascadeId": cascade}).get("steps") or []


def read_csrf_token(port, secure=True):
    """The app embeds its token in the page it serves on `GET /`."""
    context = None
    if secure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    scheme = "https" if secure else "http"
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/", timeout=10, context=context) as response:
            page = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        raise Invalid(f"读不到 Antigravity 页面（端口 {port}）：{error}") from None
    match = re.search(r"window\.__APP_CONFIG__\s*=\s*(\{.*?\});", page)
    if not match:
        raise Invalid("Antigravity 页面没有 __APP_CONFIG__，无法取得 token。")
    token = json.loads(match.group(1)).get("csrfToken")
    if not token:
        raise Invalid("Antigravity 页面没有 csrfToken。")
    return token


def activity_of(steps, seen=None):
    """Map trajectory steps to the relay's activity shape.

    `seen` is the set of indices already reported, so a poll returns only what
    the app produced since the last one. Steps are addressed by index because
    the trajectory is append-only.
    """
    seen = seen or set()
    events = []
    for index, step in enumerate(steps):
        if index in seen:
            continue
        kind = step.get("type")
        metadata = step.get("metadata") or {}
        tool = metadata.get("toolCall") or {}
        error = (step.get("errorMessage") or {}).get("error") or {}
        # An MCP call reports its failure in `error.shortError` and mirrors it in
        # `mcpTool.resultString`; `errorMessage` is empty for these steps. Reading
        # only the latter made a failed call look like a successful one.
        failure = step.get("error") or {}
        if kind == MCP_STEP:
            call = (step.get("mcpTool") or {}).get("toolCall") or {}
            try:
                arguments = json.loads(call.get("argumentsJson") or "{}")
            except ValueError:
                arguments = {}
            failed = step.get("status") != "CORTEX_STEP_STATUS_DONE"
            events.append({
                "type": "mcp/call",
                "index": index,
                "data": {
                    "server": (step.get("mcpTool") or {}).get("serverName"),
                    "tool": arguments.get("ToolName") or call.get("name"),
                    "arguments": arguments.get("Arguments"),
                    "status": step.get("status"),
                    "error": (failure.get("shortError") or step.get("mcpTool", {}).get("resultString"))
                             if failed else None,
                    "source": metadata.get("source"),
                },
            })
        elif kind == TOOL_STEP and tool:
            events.append({
                "type": "tool/call",
                "index": index,
                "data": {
                    "name": tool.get("name"),
                    "call_id": tool.get("id"),
                    "arguments": tool.get("argumentsJson"),
                    "status": step.get("status"),
                    "source": metadata.get("source"),
                },
            })
        elif kind == ASSISTANT_STEP:
            events.append({"type": "assistant/message", "index": index, "data": {"status": step.get("status")}})
        elif kind == USER_STEP:
            events.append({"type": "user/message", "index": index, "data": {"status": step.get("status")}})
        elif kind == ERROR_STEP:
            events.append({
                "type": "error",
                "index": index,
                "data": {
                    "message": error.get("modelErrorMessage") or error.get("shortError"),
                    "user_message": error.get("userErrorMessage"),
                },
            })
        else:
            events.append({"type": "step", "index": index, "data": {"name": kind, "status": step.get("status")}})
    return events


def finished(steps):
    """True only when the newest work in the trajectory is settled.

    Walking backwards until *any* done assistant message is found would call a
    turn finished while a newer tool call is still pending, and a stale error
    from an earlier attempt would end a turn that has since recovered. So the
    judgement starts at the tail and stops at the first step that settles the
    question.

    Returning False when unsure is deliberate: a caller polling again costs one
    round trip, while calling a running turn finished stops the relay from
    reporting what the app actually produced.
    """
    if not steps:
        return False
    for step in reversed(steps):
        kind = step.get("type")
        status = step.get("status")
        if kind == USER_STEP:
            # The turn never got going, or it is still queued.
            return False
        if kind == TOOL_STEP and status != "CORTEX_STEP_STATUS_DONE":
            # A tool that has not finished: anything before it is older news.
            return False
        if kind == ERROR_STEP:
            return True
        if kind == ASSISTANT_STEP:
            if status == "CORTEX_STEP_STATUS_DONE":
                return True
            return False
    return False


def cascades(server):
    """The app's own cascade list, shaped for session discovery.

    `GetAllCascadeTrajectories` is the only listing the language server exposes.
    A cascade advertises the workspace folders it was opened on, which is what
    the binding check compares, so an entry without one cannot be bound.
    """
    summaries = (server.call("GetAllCascadeTrajectories", {}) or {}).get("trajectorySummaries") or {}
    found = []
    for cascade_id, summary in summaries.items():
        folders = []
        for workspace in summary.get("workspaces") or []:
            uri = workspace.get("workspaceFolderAbsoluteUri") or ""
            if uri.startswith("file:///"):
                folders.append(uri[len("file:///"):].replace("/", "\\"))
        found.append({
            "id": cascade_id,
            "status": summary.get("status"),
            "title": (summary.get("annotations") or {}).get("title") or summary.get("summary") or "",
            "steps": summary.get("stepCount") or 0,
            "modified": summary.get("lastModifiedTime"),
            "workspace": folders[0] if folders else None,
            "workspaces": folders,
            "app": "antigravity",
        })
    return found


def locate(running_ports, preferred=None):
    """Pick the language server an instance actually exposed.

    Callers pass the ports they observed listening for the app's own processes,
    so this never invents a port or scans the machine.
    """
    for port in running_ports or []:
        if preferred and int(port) != int(preferred):
            continue
        try:
            if read_csrf_token(port, secure=False):
                return int(port)
        except Invalid:
            continue
    raise Invalid("没有发现可用的 Antigravity 语言服务器端口。")

def antigravity_ports():
    """Candidate language-server ports, derived from the app's own published port.

    The app writes its CDP port to `DevToolsActivePort`; the language server and
    its gRPC sibling sit on the nearby ports the same install opened. Candidates
    are only tried, never assumed: `locate` accepts one solely when it answers
    with a CSRF-bearing page, so a wrong guess costs a request, not a connection
    to something else.
    """
    import os
    from pathlib import Path
    home = Path(os.environ.get("APPDATA", "")) / "Antigravity"
    seeds = []
    path = home / "DevToolsActivePort"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            text = line.strip()
            if text.isdigit():
                seeds.append(int(text))
    ordered, seen = [], set()
    for seed in seeds:
        for offset in (5, 4, 1, 2, 3, 6, 7):
            port = seed + offset
            if port not in seen:
                seen.add(port)
                ordered.append(port)
    return ordered
