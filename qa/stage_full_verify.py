"""End-to-end check of the 0.1.8 stage path against a live panel.

Not an isolated bridge-permission test: it drives the real panel over HTTP for the
user's own actions and the real packaged MCP bridge for the members', then reads
the persisted record back through both the panel state and `relay_stages`.

Usage: python qa/stage_full_verify.py <base-url> <data-dir>
Creates a fixture project, exercises it, then closes and recycles it.
"""

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = sys.argv[3] if len(sys.argv) > 3 else "0.1.9"

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "release/StageRelay-0.1.9/Stage Relay/StageRelayBridge.exe"
BASE = sys.argv[1]
DATA = Path(sys.argv[2])
PORT = BASE.rsplit(":", 1)[1]
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:300]) if detail else ""))


class Panel:
    def __init__(self, base):
        self.base = base
        request = urllib.request.Request(base + "/")
        with urllib.request.urlopen(request) as response:
            cookie = response.headers.get("Set-Cookie", "")
            self.cookie = cookie.split(";")[0]
            self.cookie = self.cookie.split(" ", 1)[-1] if " " in self.cookie else self.cookie
            response.read()

    def call(self, action, **args):
        body = json.dumps({"action": action, "args": args}).encode()
        request = urllib.request.Request(
            self.base + "/api/action", data=body,
            headers={"Cookie": self.cookie, "Content-Type": "application/json",
                     "X-Relay-Client": "panel"})
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            return {"error": json.load(exc).get("error", str(exc))}

    def state(self):
        request = urllib.request.Request(self.base + "/api/state",
                                         headers={"Cookie": self.cookie})
        with urllib.request.urlopen(request) as response:
            return json.load(response)


def bridge(agent, calls):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "stage-verify", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    for number, (name, args) in enumerate(calls):
        requests.append({"jsonrpc": "2.0", "id": number + 2, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
    completed = subprocess.run(
        [str(BRIDGE), "--agent", agent, "--port", PORT, "--data-dir", str(DATA)],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        text=True, encoding="utf-8", capture_output=True, timeout=90, cwd=ROOT)
    if completed.returncode:
        raise SystemExit("bridge failed: " + completed.stderr)
    replies = [json.loads(line) for line in completed.stdout.splitlines()][1:]
    out = []
    for reply in replies:
        result = reply.get("result", {})
        text = result.get("content", [{}])[0].get("text", "")
        out.append((result.get("isError", True), text))
    return out


def catalogue():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "stage-verify", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    completed = subprocess.run(
        [str(BRIDGE), "--agent", "deepseek", "--port", PORT, "--data-dir", str(DATA)],
        input="\n".join(json.dumps(r) for r in requests) + "\n",
        text=True, encoding="utf-8", capture_output=True, timeout=90, cwd=ROOT)
    if completed.returncode:
        raise SystemExit("bridge failed: " + completed.stderr)
    listing = [json.loads(line) for line in completed.stdout.splitlines()][-1]
    tools = listing["result"]["tools"]
    return {t["name"]: t for t in tools}


panel = Panel(BASE)
health = json.load(urllib.request.urlopen(BASE + "/api/health"))
check("panel is the built version", health["version"] == VERSION, health)

tools = catalogue()
check("catalogue advertises relay_stages", "relay_stages" in tools, sorted(tools))
stage_tool = tools.get("relay_stages", {})
check("relay_stages is read-only",
      stage_tool.get("annotations", {}).get("readOnlyHint") is True, stage_tool.get("annotations"))
check("catalogue advertises issue-scoped stage submit",
      "issue_id" in tools.get("relay_stage_summary", {}).get("inputSchema", {}).get("properties", {}),
      tools.get("relay_stage_summary", {}).get("inputSchema"))

created = panel.call("project_create", title="0.1.8 阶段汇总核验", goal="验证阶段汇总的真实路径",
                     workspace=str(ROOT / "qa/stage-verify-workspace"))
project = created["result"]["id"]
check("fixture project created", bool(project), created.get("error"))
panel.call("assign", project_id=project,
           assignments={"planner": "codex", "executor": "deepseek", "reporter": "codex"})
task = panel.call("task_create", project_id=project, title="核验板块", owner="deepseek",
                  acceptance="命令与结果可核对")["result"]["id"]
panel.call("dispatch", project_id=project)
report = bridge("deepseek", [("relay_report", {"project_id": project, "task_id": task,
                                              "status": "review",
                                              "evidence": "真实桥提交：python -m pytest tests -q → 169 passed"})])[0]
check("member reports over the real bridge", not report[0], report[1][:200])
review = bridge("codex", [("relay_review", {"project_id": project, "task_id": task,
                                           "approved": True,
                                           "evidence": "独立核对：pytest 输出与文件改动一致"})])[0]
check("supervisor reviews over the real bridge", not review[0], review[1][:200])
issue = panel.call("issue_create", project_id=project, title="阶段一：工具发现与正文查看",
                   body="议题范围覆盖工具目录、正文查看与全部历史",
                   members=["codex", "deepseek"],
                   options=[{"title": "按阶段汇总", "detail": "逐个议题记录"},
                            {"title": "只做最终汇总", "detail": "不记录过程"}])["result"]
panel.call("resolve", project_id=project, issue_id=issue["id"], choice=issue["options"][0]["id"])

# The combined role records the stage in one call, with no prior stage_id.
empty = bridge("codex", [("relay_request_summary", {"project_id": project,
                                                   "issue_id": issue["id"]})])[0]
check("supervisor==reporter without body is refused with a usable message",
      empty[0] and "body" in empty[1], empty[1][:200])
made = bridge("codex", [("relay_request_summary", {
    "project_id": project, "issue_id": issue["id"],
    "body": "阶段一完成：工具目录已含 relay_stages；正文与全部历史可从面板打开。"})])[0]
check("supervisor==reporter records the stage in one call", not made[0], made[1][:300])
stage = json.loads(made[1]) if not made[0] else {}
check("the recorded stage is submitted and marked self-submitted",
      stage.get("status") == "submitted" and stage.get("self_submitted") is True, stage.get("status"))

listing = bridge("codex", [("relay_stages", {"project_id": project})])[0]
view = json.loads(listing[1]) if not listing[0] else {}
check("relay_stages returns the stage with its body",
      view.get("total") == 1 and "阶段一完成" in (view.get("stages") or [{}])[0].get("body", ""),
      view.get("total"))
check("relay_stages reports no outstanding resolved issue",
      view.get("pending_stages") == [], view.get("pending_stages"))

# The same issue cannot be summarised twice behind the panel's back.
twice = bridge("codex", [("relay_stage_summary", {"project_id": project,
                                                 "issue_id": issue["id"],
                                                 "body": "重复提交"})])[0]
check("a second direct submission is refused", twice[0], twice[1][:200])

state = panel.state()
saved = next(p for p in state["projects"] if p["id"] == project)
check("the panel state carries the persisted stage", len(saved.get("stages", [])) == 1,
      [(s["status"], s["revision"]) for s in saved.get("stages", [])])
check("the stage body reached the panel too",
      "阶段一完成" in (saved.get("stages") or [{}])[0].get("body", ""))

# A reporter who is not the supervisor records a stage from the issue alone.
panel.call("assign", project_id=project,
           assignments={"planner": "codex", "executor": "deepseek", "reporter": "antigravity"})
second = panel.call("issue_create", project_id=project, title="阶段二：直接按议题记录",
                    body="验证不需要先请求", members=["codex", "deepseek"],
                    options=[{"title": "直接提交", "detail": "报告人自己建立阶段记录"},
                             {"title": "等待请求", "detail": "监督先发起"}])["result"]
panel.call("resolve", project_id=project, issue_id=second["id"], choice=second["options"][0]["id"])
direct = bridge("antigravity", [("relay_stage_summary", {
    "project_id": project, "issue_id": second["id"],
    "body": "阶段二：报告人直接按议题建立并提交了阶段记录。"})])[0]
check("a reporter records a stage from the issue alone", not direct[0], direct[1][:300])
again = bridge("antigravity", [("relay_stage_summary", {
    "project_id": project, "issue_id": second["id"], "body": "再来一次"})])[0]
check("repeating it is refused rather than adding a revision", again[0], again[1][:200])

final = bridge("codex", [("relay_stages", {"project_id": project})])[0]
final_view = json.loads(final[1]) if not final[0] else {}
check("both stages persist with their own bodies",
      final_view.get("total") == 2
      and all(s.get("body") for s in final_view.get("stages", [])),
      [(s["issue_id"][:12], s["revision"], bool(s.get("body"))) for s in final_view.get("stages", [])])
state = panel.state()
saved = next(p for p in state["projects"] if p["id"] == project)
check("panel shows both stages", len(saved.get("stages", [])) == 2, len(saved.get("stages", [])))

# Leave no fixture behind: close, then recycle through the normal flow.
panel.call("project_close", project_id=project, retention_days=1)
panel.call("project_forget", project_id=project)
state = panel.state()
check("fixture project removed from the active list",
      all(p["id"] != project for p in state["projects"]),
      [p["id"] for p in state["projects"]])

failed = [r for r in results if not r["ok"]]
print(json.dumps({"total": len(results), "failed": len(failed),
                  "failures": failed}, ensure_ascii=False, indent=1))
Path(ROOT / "qa/stage-018-verify.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
sys.exit(1 if failed else 0)
