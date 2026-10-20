"""Local collaboration GUI. Models start only on explicit task submission."""
import argparse
import hashlib
import json
import os
import re
import secrets
import time
import webbrowser
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from relaylib.review import read_confirmation

from relaylib.settings import Settings, SettingsError
from relaylib import __version__
from relaylib.catalog import Catalog
from relaylib.contracts import RelayError, validate, fail
from relaylib.plugin import JobStore, HANDOFF
from relaylib.runtime import atomic_json, FileLock
from relaylib.diagnostics import describe_failure
from relaylib import checks
from relaylib import subagents
from relaylib import corrections
from relaylib import assistance

ROOT = Path(__file__).resolve().parent
PHASE = re.compile(r"\d{3,6}-(planner|worker|reviewer)")
MAX_PHASES = 80
ERROR_HELP = {
    "project_missing": "项目目录不存在或不是文件夹，可能已被移动或删除。请填写一个实际存在的 Git 项目根目录。",
    "git_required": "Git 无法访问这个项目。请确认它是有效的 Git 仓库，并检查目录权限。",
    "git_no_commit": "这个 Git 仓库还没有首次提交，请先建立项目基线后再开始接力。",
    "git_missing": "面板进程找不到 Git。请安装 Git，或让启动面板的程序能找到 Git 后重新打开。",
    "git_timeout": "Git 项目检查超时，请确认项目可正常访问后重试。",
    "project_root": "请选择 Git 仓库的根目录；当前选择的是仓库内的子文件夹。",
    "dirty_project": "项目中有未提交的改动。请先处理已有改动，或选择一个干净的独立项目。",
    "project_denied": "项目目录不在当前面板允许的范围内，请选择已配置允许目录下的项目。",
    "unsafe_path": "运行记录不能放在目标项目仓库内，请选择一个独立的项目目录。",
    "busy": "任务仍在排队或运行，请等待进程结束后再确认、审核或管理记录。",
    "review_stale": "执行结果已变化，请刷新后核对最新回答再操作。",
    "help_stale": "这条求助已经变化或已答复，请读取当前问题后再回复。",
    "run_missing": "这条任务记录已不存在，请刷新列表。",
}


def relay_error(error):
    diagnostic = describe_failure(error, {"stage": "提交任务前检查"})
    if error.code in ERROR_HELP:
        diagnostic.update(message=ERROR_HELP[error.code], action=ERROR_HELP[error.code])
    return {"error": diagnostic["message"], "code": error.code, "diagnostic": diagnostic}


def safe_file(directory, name):
    candidate = directory / name
    resolved = candidate.resolve()
    if not resolved.is_relative_to(directory.resolve()) or not resolved.is_file():
        return None
    return resolved


def text_file(directory, name, limit=200_000, tail=False):
    path = safe_file(directory, name)
    if path is None:
        return ""
    try:
        with path.open("rb") as stream:
            if tail and path.stat().st_size > limit:
                stream.seek(-limit, 2)
            data = stream.read(limit)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def json_file(directory, name):
    try:
        value = json.loads(text_file(directory, name))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def request_activity(directory, budget):
    """Read public request sizes, never bodies; warnings do not stop execution."""
    path = safe_file(directory, "requests.jsonl")
    rows = []
    for line in text_file(directory, "requests.jsonl", limit=256_000, tail=True).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") in ("request_forwarded", "request_denied"):
            rows.append(row)
    forwarded = [row for row in rows if row["event"] == "request_forwarded"]
    count = sum(row.get("forwarded", 0) for row in budget.get("roles", {}).values())
    latest = next((row for row in reversed(rows) if type(row.get("body_bytes")) is int), {})
    first = forwarded[0] if forwarded and path and path.stat().st_size <= 256_000 else {}
    size = latest.get("body_bytes")
    warnings = []
    if count >= 10:
        warnings.append("已累计发送多次模型请求。请核对最近的输出是否仍在推进任务，避免反复做范围外的验证。")
    if size is not None and size >= 1_000_000:
        warnings.append("本次上下文较大，可能增加模型等待和费用。此处仅提醒，不会截断输入、减少输出或停止任务。")
    denied = next((row for row in reversed(rows) if row["event"] == "request_denied" and row.get("reason") == "input_limit"), {})
    return {"forwarded_requests": count, "last_request_bytes": size,
            "first_request_bytes": first.get("body_bytes"), "warnings": warnings,
            "local_rejection": {key: denied[key] for key in ("body_bytes", "limit_bytes", "phase") if key in denied}}


def public_events(directory):
    """Bounded public CLI events; do not display reasoning/internal trace items."""
    events, positions, usage = [], {}, []
    raw = text_file(directory, "events.jsonl", limit=256_000, tail=True)
    for line in raw.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
        except ValueError:
            continue  # A last line may still be being written.
        kind = event.get("type", "")
        if kind == "turn.completed":
            usage.append(event.get("usage"))
        item = event.get("item")
        if isinstance(item, dict) and kind in ("item.started", "item.updated", "item.completed"):
            item_kind = item.get("type")
            if item_kind == "agent_message":
                row = {"kind": "message", "text": str(item.get("text", ""))[:24_000]}
            elif item_kind == "command_execution":
                row = {"kind": "command", "text": str(item.get("command", ""))[:8_000],
                       "output": str(item.get("aggregated_output", ""))[-16_000:],
                       "status": item.get("status"), "exit_code": item.get("exit_code")}
            elif item_kind == "file_change":
                row = {"kind": "files", "text": json.dumps(item.get("changes", []), ensure_ascii=False)[:16_000],
                       "status": item.get("status")}
            elif item_kind == "mcp_tool_call" and item.get("server") == "cua_repl" and item.get("status") == "completed":
                # Only expose a capability inventory, never MCP documentation or arbitrary results.
                inventory = None
                result = item.get("result")
                if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                    continue
                for content in result["content"]:
                    if not isinstance(content, dict) or content.get("type") != "text":
                        continue
                    if not isinstance(content.get("text"), str):
                        continue
                    try:
                        candidate = json.loads(content.get("text", ""))
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and isinstance(candidate.get("browsers"), list):
                        inventory = candidate
                if inventory is None:
                    continue
                available = bool(inventory["browsers"])
                row = {"kind": "capability", "browser_available": available,
                       "text": "执行端发现可用浏览器。" if available else "执行端未发现可用浏览器；视觉验证需要交回当前 Codex。"}
            else:
                continue
            key = item.get("id")
            if key is not None and key in positions:
                events[positions[key]] = row
            else:
                if key is not None:
                    positions[key] = len(events)
                events.append(row)
        elif kind in ("error", "turn.failed"):
            events.append({"kind": "error", "text": json.dumps(event.get("error", event.get("message", kind)), ensure_ascii=False)[:8_000]})
    return events[-60:], usage


def handoff_input(prompt):
    """Extract the JSON actually passed in this phase's persisted stdin."""
    prompt = prompt.replace("\r\n", "\n")
    sections = {}
    markers = {"plan": "CURRENT PLAN:\n",
               "previous_result": "LAST RESULT (bounded handoff, not full history):\n",
               "checks": "INDEPENDENT CHECK RESULTS:\n"}
    # Find markers after configured checks, so marker-like text in the user's
    # task cannot be misidentified as an orchestrator handoff.
    checks_marker = "CONFIGURED CHECKS (ID -> argv):\n"
    index = prompt.rfind(checks_marker)
    start = prompt.find("\n", index + len(checks_marker)) + 1 if index >= 0 else len(prompt)
    for name, marker in markers.items():
        index = prompt.find(marker, start)
        if index >= 0:
            try:
                sections[name] = json.JSONDecoder().raw_decode(prompt[index + len(marker):].lstrip())[0]
            except ValueError:
                pass
    return sections


class Store:
    def __init__(self, runs_root=None, run_dir=None):
        self.root = Path(runs_root or ROOT / "runs").resolve()
        self.explicit = Path(run_dir).resolve() if run_dir else None

    @staticmethod
    def identity(path):
        return hashlib.sha256(str(path).encode()).hexdigest()[:24]

    def _directories(self):
        if self.explicit:
            return [self.explicit] if safe_file(self.explicit, "state.json") else []
        if not self.root.is_dir():
            return []
        found = []
        # Only direct run dirs and their standard relay child; never crawl
        # project repositories or serve arbitrary paths from a URL.
        for child in self.root.iterdir():
            if not child.is_dir() or not child.resolve().is_relative_to(self.root):
                continue
            candidate = child if safe_file(child, "state.json") else child / "relay"
            if candidate.resolve().is_relative_to(self.root) and (safe_file(candidate, "state.json") or
                    (safe_file(child, "job.json") and safe_file(child, "service.json"))):
                found.append(candidate.resolve())
        return found

    def deleted(self):
        return json_file(self.root, ".deleted-runs.json").get("deleted", {})

    def directories(self):
        hidden = self.deleted()
        return [path for path in self._directories() if self.identity(path) not in hidden]

    @staticmethod
    def active(path):
        job = safe_file(path.parent, "job.json") if path.name == "relay" else None
        service = json_file(path.parent, "service.json") if job else {}
        if service:
            return service.get("status") in ("queued", "running") and time.time() - service.get("time", 0) < 15
        return json_file(path, "state.json").get("status") in ("running", "ready")

    def change_history(self, identity, action):
        self.change_history_batch([identity], action)
        return {"id": identity, "action": action, "generation_requests": 0}

    def last_deleted_ids(self):
        data = json_file(self.root, ".deleted-runs.json")
        hidden = data.get("deleted", {})
        batch = [identity for identity in data.get("last_batch", []) if identity in hidden]
        if not batch and hidden:
            batch = [max(hidden, key=lambda key: hidden[key].get("time", 0))]
        return batch

    def change_history_batch(self, identities, action):
        if (not isinstance(identities, list) or not 1 <= len(identities) <= 10000 or
                any(not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{24}", identity) for identity in identities)
                or len(set(identities)) != len(identities) or action not in ("delete", "restore")):
            fail("protocol_error", "Invalid history operation")
        paths = {self.identity(path): path for path in self._directories()}
        if any(identity not in paths for identity in identities):
            fail("run_missing", "Run does not exist")
        # Only change the panel index. Keep checkpoints and idempotency keys so
        # deleting a row cannot cause a repeated submission to call models again.
        with FileLock(self.root / ".history.lock"):
            with ExitStack() as locks:
                for identity in sorted(identities):
                    path = paths[identity]
                    if action == "delete" and self.active(path):
                        fail("busy", "所选记录中有任务正在运行，请刷新后重新选择。此次未删除任何记录。")
                    if path.name == "relay" and safe_file(path.parent, "job.json"):
                        locks.enter_context(FileLock(path.parent / "service.lock"))
                    if action == "delete" and self.active(path):
                        fail("busy", "所选记录中有任务正在运行，请刷新后重新选择。此次未删除任何记录。")
                hidden = self.deleted()
                last_batch = self.last_deleted_ids()
                now = time.time()
                if action == "delete":
                    for identity in identities:
                        hidden[identity] = {"time": now}
                    last_batch = identities
                else:
                    for identity in identities:
                        hidden.pop(identity, None)
                    last_batch = [identity for identity in last_batch if identity in hidden]
                atomic_json(self.root / ".deleted-runs.json", {"version": 1, "deleted": hidden, "last_batch": last_batch})
        return {"ids": identities, "action": action, "count": len(identities), "generation_requests": 0}

    @staticmethod
    def metadata(path):
        job = json_file(path.parent, "job.json") if path.name == "relay" else {}
        operation = json_file(path.parent, "operation.json") if job else {}
        request = job.get("title") or operation.get("request") or text_file(path, "request.md")
        if not request or request == "Execute the caller-supplied plan.":
            request = (operation.get("plan") or {}).get("goal") or json_file(path, "plan.json").get("goal") or request
        return job, str(request or "未命名任务")

    def runs(self):
        rows = []
        for path in self.directories():
            state = json_file(path, "state.json")
            job, request = self.metadata(path)
            service = json_file(path.parent, "service.json") if job else {}
            config = json_file(path, "config.json") or (json_file(path.parent, "config.json") if job else {})
            command = config.get("codex_command", [])
            mock = isinstance(command, list) and any("fake_codex.py" in str(part) for part in command)
            confirmation = read_confirmation(path, state) if state.get("last_result") and safe_file(path, "plan.json") else None
            rows.append({"id": self.identity(path), "name": path.parent.name if path.name == "relay" else path.name,
                         "title": request.splitlines()[0][:120],
                         "status": "user_confirmed" if confirmation and not confirmation["stale"] and not self.active(path) else state.get("status", service.get("status", "unreadable")),
                         "updated_at": state.get("updated_at", service.get("time", job.get("created_at", 0))),
                         "can_delete": not self.active(path),
                         "mode": "mock" if mock else "recorded"})
        return sorted(rows, key=lambda row: row["updated_at"] or 0, reverse=True)

    def snapshot(self, identity):
        directory = next((p for p in self.directories() if self.identity(p) == identity), None)
        if directory is None:
            return None
        state, config = json_file(directory, "state.json"), json_file(directory, "config.json")
        job, request = self.metadata(directory)
        service = json_file(directory.parent, "service.json") if job else {}
        if not config and job:
            config = json_file(directory.parent, "config.json")
        failure = state.get("failure") or service.get("diagnostic")
        if not failure and service.get("error"):
            failure = describe_failure(RelayError(service["error"], ""), logs=[directory.parent / "service.stderr.log"])
        accepted = {a.get("directory") for a in state.get("attempts", []) if isinstance(a, dict)}
        pending = state.get("pending") or {}
        phases, handoffs = [], []
        paths = sorted(p for p in (directory.iterdir() if directory.is_dir() else []) if PHASE.fullmatch(p.name) and p.is_dir()
                       and p.resolve().is_relative_to(directory))[-MAX_PHASES:]
        for path in paths:
            role = PHASE.fullmatch(path.name).group(1)
            finish = json_file(path, "finished.json")
            events, event_usage = public_events(path)
            usage = finish.get("usage", {}).get("usage", event_usage)
            prompt = text_file(path, "input.txt")
            payload = handoff_input(prompt)
            is_pending = pending.get("directory") == path.name
            status = "accepted" if path.name in accepted else (
                "failed" if finish.get("failure") or finish.get("exit_code", 0) else (
                "finished" if finish else ("running" if is_pending and state.get("status") == "running" else "interrupted")))
            # state=running can survive a crash; this is checkpoint state, not
            # a claim that a process or an API request is currently alive.
            phases.append({"id": path.name, "role": role, "status": status, "model": finish.get("model"),
                           "subagents": finish.get("subagents") or subagents.lifecycle(path / "events.jsonl", config.get("worker", {}).get("model")),
                           "routed_usage": finish.get("routed_usage") or subagents.routed_usage(directory / "requests.jsonl", path.name),
                           "events": events, "usage": usage, "input": prompt,
                           "response": json_file(path, "response.json"),
                           "stderr": text_file(path, "stderr.log", limit=8_000, tail=True),
                           "failure": finish.get("failure"), "elapsed": finish.get("elapsed_seconds")})
            if role == "worker" and payload.get("plan"):
                if payload.get("checks"):
                    sender, title = "本地检查", "修复反馈 → 执行 agent"
                else:
                    sender = "调用方 agent" if state.get("plan_source") == "caller" else "规划 agent"
                    title = sender + "的计划 → 执行 agent"
            elif role == "reviewer":
                sender, title = "执行 agent", "执行结果与检查证据 → 审核模型" + (" · " + finish["model"] if finish.get("model") else "")
            elif role == "planner" and payload.get("previous_result"):
                sender, title = "执行 agent", "纠偏请求 → 规划 agent"
            elif role == "worker":
                sender, title = "交接文件", "交接输入 → 执行 agent（计划段暂不可读）"
            else:
                sender, title = "用户", "任务 → 规划 agent"
            handoffs.append({"phase": path.name, "from": sender, "to": role, "title": title,
                             "status": status, "payload": payload or {"request": text_file(directory, "request.md")},
                             "evidence": f"{path.name}/input.txt"})
            if role in ("worker", "reviewer") and path.name in accepted:
                response = json_file(path, "response.json")
                receiver = "调用方 agent" if state.get("plan_source") == "caller" else "本地调度器"
                handoffs.append({"phase": path.name + "-return", "from": (finish.get("model") or "审核模型") if role == "reviewer" else "执行 agent", "to": receiver,
                                 "title": ("审核结论 → " if role == "reviewer" else "执行结果 → ") + receiver, "status": status, "payload": response,
                                 "evidence": f"{path.name}/response.json"})
        budget = json_file(directory, "budget.json")
        activity = request_activity(directory, budget)
        if failure and failure.get("code") == "input_limit" and activity["local_rejection"]:
            # Correct old diagnostics from public audit evidence without rewriting history.
            context = {**failure, **activity["local_rejection"], "stage": "本地请求大小检查",
                       "local_status": 413, "request_forwarded": False}
            failure = describe_failure(RelayError("input_limit", ""), context, failure.get("logs"))
        roles = {}
        for role in ("planner", "worker"):
            spec = config.get(role, {})
            roles[role] = {"model": spec.get("model", "未记录"), "max_requests": spec.get("max_requests"),
                           "requests": budget.get("roles", {}).get(role, {}).get("forwarded")}
            if role == "planner" and state.get("plan_source") == "caller":
                roles[role].update(model="调用方 agent · 复用已有方案", requests=0, reused=True)
        from relaylib.review import read_review, revision as review_revision
        review = read_review(directory, state) if (directory / "plan.json").is_file() and state.get("last_result") else None
        target = json_file(directory, "reviewer-target.json")
        reviewer = target.get("spec", config.get("planner", {}))
        reviewer_role = "reviewer" if target.get("selection") else "planner"
        roles["reviewer"] = {"model": reviewer.get("model", "未记录"), "max_requests": reviewer.get("max_requests"),
                             "requests": budget.get("roles", {}).get(reviewer_role, {}).get("forwarded")}
        confirmation = read_confirmation(directory, state) if state.get("last_result") and (directory / "plan.json").is_file() else None
        active = self.active(directory)
        confirmed = confirmation and not confirmation["stale"] and not active
        can_confirm = bool(job and not active and not failure and not state.get("pending") and
                           state.get("status") in ("completed", "needs_manual_verification", "exhausted", "review_paused") and
                           (state.get("last_result") or {}).get("status") == "completed")
        return {"id": identity, "job_id": directory.parent.name if job else None,
                "help_request": state.get("help_request") if state.get("status") == "awaiting_help" else None,
                "can_answer_help": bool(job and not active and state.get("status") == "awaiting_help"),
                "caller_help_request": (assistance.caller_request(directory.parent.name, state["help_request"], state["project"])
                                        + f"\n若求助工具未加载，读取 {ROOT / '.mcp.json'} 的本机启动参数，使用同一 mcp_server.py 的 --answer-help <JSON文件> 写回答复。"
                                        if job and state.get("status") == "awaiting_help" and state.get("help_request") else None),
                "compaction_count": state.get("compaction_count", 0),
                "context_events": state.get("context_events", []),
                "confirmation": confirmation, "can_confirm": can_confirm,
                "review": review, "review_revision": review_revision(directory, state) if state.get("last_result") else None,
                "plan_source": state.get("plan_source", "api"), "review_required": state.get("review_required", False),
                "path": str(directory), "execution_state": state.get("status"),
                "state": "user_confirmed" if confirmed else state.get("status", service.get("status", "unreadable")),
                "next": state.get("next"), "failure": failure, "title": request.splitlines()[0][:120],
                "can_delete": not self.active(directory),
                "project": state.get("project", job.get("project")), "updated_at": state.get("updated_at", service.get("time")),
                "request": request, "roles": roles, "phases": phases,
                "request_activity": activity,
                "review_state": state.get("review_state"), "review_cycle": corrections.public_cycle(state),
                "review_record_wiring": str(ROOT / ".mcp.json"),
                "correction_history": corrections.history(directory),
                "pause_requested": (directory / "correction-pause.json").is_file(),
                "subagents_policy": config.get("subagents", {"mode": "off"}),
                "handoffs": handoffs, "checks": state.get("validation"), "configured_checks": list(config.get("checks", {})),
                "patch": text_file(directory, "changes.patch", limit=60_000),
                "observed_at": time.time(), "observer_model_calls": 0,
                "limits": "日志仅展示最近 256 KB 中的 60 条公开事件；阶段最多 80 个，单文件最多 200 KB。"}


def make_server(store, port=0, settings=None, jobs=None):
    settings = settings or Settings(ROOT / "config.local.json")
    catalog = Catalog()
    prefix = "/" + secrets.token_urlsafe(24)
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/panel.css": ("panel.css", "text/css; charset=utf-8"),
              "/panel.js": ("panel.js", "text/javascript; charset=utf-8")}
    assets["/settings.js"] = ("settings.js", "text/javascript; charset=utf-8")
    assets["/compose.js"] = ("compose.js", "text/javascript; charset=utf-8")
    assets["/icon.svg"] = ("../assets/icon.svg", "image/svg+xml")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data, mime="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.reply(403, {"error": "Local observer only"})
                return
            path = urlsplit(self.path).path
            if not path.startswith(prefix + "/"):
                self.reply(404, {"error": "Not found"})
                return
            suffix = path[len(prefix):]
            try:
                if suffix in assets:
                    name, mime = assets[suffix]
                    self.reply(200, (ROOT / "panel" / name).read_bytes(), mime)
                elif suffix == "/api/runs":
                    deleted = store.deleted()
                    latest = max(deleted, key=lambda key: deleted[key].get("time", 0)) if deleted else None
                    self.reply(200, {"runs": store.runs(), "deleted_count": len(deleted), "last_deleted": latest,
                                     "last_deleted_ids": store.last_deleted_ids()})
                elif suffix == "/api/settings":
                    self.reply(200, settings.view())
                elif suffix == "/api/health":
                    self.reply(200, {"instance": self.server.instance})
                elif suffix == "/api/launch":
                    view = settings.view()
                    roles = view.get("roles", {})
                    from relaylib.panel_runtime import ensure_shortcut, launch_hint
                    shortcut = None
                    if jobs:
                        ensure_shortcut(jobs)
                        shortcut = (launch_hint(jobs) or {}).get("path")
                    self.reply(200, {"enabled": jobs is not None, "projects": [str(p) for p in jobs.projects] if jobs else [],
                                     "roles": roles, "revision": view["revision"], "profiles": view["profiles"],
                                     "credential_states": view["credential_states"], "version": __version__,
                                     "shortcut": shortcut})
                elif suffix == "/api/draft":
                    draft = jobs.draft() if jobs else None
                    after = parse_qs(urlsplit(self.path).query).get("after", [None])[0]
                    identity = draft.get("id") if draft else None
                    self.reply(200, {"id": identity, "changed": False, "model_calls": 0} if after and after == identity else
                               {"draft": draft, "id": identity, "changed": True, "model_calls": 0})
                elif suffix == "/api/checks":
                    # Saved records only: reading this route never calls a model.
                    self.reply(200, checks.list_records(jobs.root if jobs else ROOT / "runs", settings))
                elif re.fullmatch(r"/api/job/[a-f0-9]{24}", suffix) and jobs:
                    row = jobs.snapshot(suffix.rsplit("/", 1)[-1])
                    row["run_id"] = Store.identity(jobs.directory(row["job_id"]) / "relay")
                    self.reply(200, row)
                elif re.fullmatch(r"/api/run/[a-f0-9]{24}", suffix):
                    value = store.snapshot(suffix.rsplit("/", 1)[-1])
                    self.reply(200 if value else 404, value or {"error": "Run not found"})
                else:
                    self.reply(404, {"error": "Not found"})
            except SettingsError as error:
                self.reply(error.status, {"error": str(error)})
            except RelayError as error:
                self.reply(400, relay_error(error))
            except (OSError, ValueError, TypeError, AttributeError):
                self.reply(503, {"error": "日志正在写入或暂不可读，稍后刷新"})

        def do_POST(self):
            def reject(status, message):
                # Drain only a bounded, immediately available body fragment.
                # Windows can discard the HTTP error with a TCP reset if a
                # small rejected POST is closed with unread bytes pending.
                length = self.headers.get("Content-Length", "")
                if length.isascii() and length.isdigit() and len(length) <= 6:
                    self.connection.settimeout(0.25)
                    try:
                        self.rfile.read1(min(int(length), 32_000))
                    except OSError:
                        pass
                self.reply(status, {"error": message})

            expected = f"127.0.0.1:{self.server.server_port}"
            if (self.headers.get("Host") != expected or
                    self.headers.get("Origin") != "http://" + expected or
                    self.headers.get("X-Relay-Panel") != "settings" or
                    self.headers.get("Sec-Fetch-Site") == "cross-site"):
                reject(403, "仅允许本地面板操作")
                return
            route = urlsplit(self.path).path
            if route not in tuple(prefix + name for name in ("/api/settings", "/api/jobs", "/api/models", "/api/history",
                                                             "/api/worker-models", "/api/worker", "/api/check", "/api/confirm", "/api/review", "/api/correct", "/api/pause-corrections", "/api/answer-help")):
                reject(404, "Not found")
                return
            raw_length = self.headers.get("Content-Length", "")
            maximum = 2_000_000 if route.endswith(("/jobs", "/history", "/answer-help")) else 32_000
            if (not raw_length.isascii() or not raw_length.isdigit() or len(raw_length) > 7 or not 0 < int(raw_length) <= maximum or
                    self.headers.get("Transfer-Encoding") or
                    self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                reject(400, "JSON 请求格式或大小不正确")
                return
            self.connection.settimeout(5)
            try:
                value = json.loads(self.rfile.read(int(raw_length)))
                if route.endswith("/answer-help"):
                    if jobs is None:
                        raise SettingsError("当前入口仅供查看。", 403)
                    validate(value, {"type": "object", "properties": {
                        "job_id": {"type": "string", "pattern": "[a-f0-9]{24}"},
                        "help_revision": {"type": "string", "pattern": "[a-f0-9]{64}"},
                        "answer": {"type": "string", "minLength": 1, "maxLength": 100000}},
                        "required": ["job_id", "help_revision", "answer"], "additionalProperties": False})
                    result = jobs.answer_help(**value)
                elif route.endswith("/pause-corrections"):
                    if jobs is None:
                        raise SettingsError("当前入口仅供查看。", 403)
                    validate(value, {"type": "object", "properties": {"job_id": {"type": "string", "pattern": "[a-f0-9]{24}"}}, "required": ["job_id"], "additionalProperties": False})
                    result = jobs.pause_corrections(value["job_id"])
                elif route.endswith(("/confirm", "/review", "/correct")):
                    if jobs is None:
                        raise SettingsError("当前入口仅供查看。", 403)
                    properties = {
                        "job_id": {"type": "string", "pattern": "[a-f0-9]{24}"},
                        "result_revision": {"type": "string", "pattern": "[a-f0-9]{64}"}}
                    if route.endswith("/review"):
                        properties.update(provider_id={"type": "string", "minLength": 1, "maxLength": 80},
                                          model={"type": "string", "minLength": 1, "maxLength": 200},
                                          auto_correct={"type": "boolean"})
                    validate(value, {"type": "object", "properties": properties,
                                    "required": ["job_id", "result_revision"], "additionalProperties": False})
                    if ("provider_id" in value) != ("model" in value):
                        raise SettingsError("请选择完整的已有审核模型。")
                    if route.endswith("/confirm"):
                        result = jobs.confirm_result(**value)
                    elif route.endswith("/correct"):
                        result = jobs.correct_result(**value)
                    else:
                        reviewer = settings.reviewer(value["provider_id"], value["model"]) if "provider_id" in value else None
                        result = jobs.request_review(value["job_id"], value["result_revision"], reviewer=reviewer, auto_correct=value.get("auto_correct", True))
                elif route.endswith("/worker-models"):
                    if not isinstance(value, dict) or set(value) - {"provider_id", "refresh"}:
                        raise SettingsError("接口选择格式不正确。")
                    view = settings.view()
                    profile = next((p for p in view["profiles"] if p["id"] == value.get("provider_id")), None)
                    if profile is None:
                        raise SettingsError("所选接口不存在。")
                    result = catalog.discover({"endpoint": profile["endpoint"], "refresh": value.get("refresh", False)})
                    result.update(endpoint=profile["endpoint"].get("base_url", profile["endpoint"].get("codex_provider")),
                                  saved_count=len(profile["models"]), provider_id=profile["id"])
                elif route.endswith("/worker"):
                    result = settings.select_worker(value, catalog)
                elif route.endswith("/check"):
                    # Explicit user action: this is the only route that may call a model.
                    validate(value, {"type": "object", "properties": {
                        "provider_id": {"type": "string", "minLength": 1, "maxLength": 80},
                        "model": {"type": "string", "minLength": 1, "maxLength": 200},
                        "kind": {"type": "string", "enum": ["catalog", "generation", "compat"]}},
                        "required": ["provider_id", "model", "kind"], "additionalProperties": False})
                    result = checks.run_check(jobs.root if jobs else ROOT / "runs", settings,
                                              value["provider_id"], value["model"], value["kind"])
                elif route.endswith("/history"):
                    validate(value, {"type": "object", "properties": {"id": {"type": "string", "pattern": "[a-f0-9]{24}"},
                              "ids": {"type": "array", "minItems": 1, "maxItems": 10000,
                                      "items": {"type": "string", "pattern": "[a-f0-9]{24}"}},
                              "action": {"type": "string", "enum": ["delete", "restore"]}},
                              "required": ["action"], "additionalProperties": False})
                    if ("id" in value) == ("ids" in value):
                        raise SettingsError("请选择单条或多条记录。")
                    result = (store.change_history(value["id"], value["action"]) if "id" in value else
                              store.change_history_batch(value["ids"], value["action"]))
                elif route.endswith("/settings"):
                    result = settings.save(value)
                elif route.endswith("/models"):
                    result = catalog.discover(value)
                else:
                    if jobs is None:
                        self.reply(403, {"error": "当前入口仅供查看；请通过插件打开可启动任务的面板"})
                        return
                    validate(value, {"type": "object", "properties": {
                        "project": {"type": "string", "minLength": 1},
                        "task_key": {"type": "string", "pattern": "[A-Za-z0-9_-]{1,64}"},
                        "request": {"type": "string", "minLength": 1}, "plan": HANDOFF,
                        "draft_id": {"type": "string", "pattern": "[a-f0-9]{24}"},
                        "user_request": {"type": "string", "minLength": 1, "maxLength": 100000},
                        "review_mode": {"type": "string", "enum": ["user_choice", "auto"]},
                        "subagents": subagents.SCHEMA},
                        "required": ["project", "task_key"], "additionalProperties": False})
                    if ("plan" in value) == ("request" in value):
                        self.reply(400, {"error": "请选择完整接力或已有方案执行"})
                        return
                    if "user_request" in value and "plan" not in value:
                        raise SettingsError("原始任务指示仅用于已有方案执行。")
                    if "user_request" in value and "draft_id" not in value and (jobs.draft() or {}).get("invocation_id"):
                        fail("draft_stale", "This old panel did not identify the current invocation; refresh before starting")
                    result = jobs.submit(**value) if "plan" in value else jobs.submit_task(**value)
                    result["run_id"] = Store.identity(jobs.directory(result["job_id"]) / "relay")
                self.reply(200, result)
            except SettingsError as error:
                self.reply(error.status, {"error": str(error)})
            except RelayError as error:
                self.reply(400, relay_error(error))
            except (ValueError, TypeError):
                self.reply(400, {"error": "配置格式不正确"})
            except OSError as error:
                diagnostic = describe_failure(error, {"stage": "提交本地任务" if route.endswith("/jobs") else "保存本地配置"})
                self.reply(503, {"error": diagnostic["message"], "code": diagnostic["code"], "diagnostic": diagnostic})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.instance = secrets.token_urlsafe(24)
    server.url = f"http://127.0.0.1:{server.server_port}{prefix}/"
    return server


def main():
    parser = argparse.ArgumentParser(description="Local relay GUI; viewing is free, task submission starts configured models")
    parser.add_argument("--runs-root", default=str(ROOT / "runs"))
    parser.add_argument("--run-dir", help="Observe one explicit relay artifact directory")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--open", action="store_true", help="Open the local panel in your browser")
    parser.add_argument("--config", default=str(ROOT / "config.local.json"), help="Future-task config to edit; existing runs are unchanged")
    parser.add_argument("--projects-root", action="append", help="Allowed project roots; enables task launch")
    parser.add_argument("--control-file", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        jobs = JobStore(args.config, args.runs_root, args.projects_root) if args.projects_root else None
        server = make_server(Store(args.runs_root, args.run_dir), args.port, Settings(args.config), jobs)
    except OSError as error:
        parser.exit(2, f"Cannot open local observer: {error}. Try --port 0 for an available port.\n")
    print(server.url, flush=True)
    if args.control_file:
        atomic_json(Path(args.control_file), {"url": server.url, "pid": os.getpid(), "instance": server.instance,
                                             "identity": {"config": str(jobs.config), "projects": [str(p) for p in jobs.projects], "version": __version__}})
    print("本地协作面板：打开与查看不调用模型；点击开始任务后才执行。Ctrl+C 关闭。", flush=True)
    if args.open:
        webbrowser.open(server.url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
