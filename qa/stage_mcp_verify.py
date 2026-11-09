"""Verify the stage-summary path end to end through the real MCP bridge.

Codex's review said the MCP tool accepted `stage` but nothing downstream used it.
This drives the actual bridge subprocess the members use, so it fails if any hop
(user decision -> supervisor request -> reporter submission) is still severed.
"""
import json
import os
import subprocess
import sys
import urllib.request

BRIDGE = os.path.abspath(r"release\StageRelay-0.1.7\Stage Relay\StageRelayBridge.exe")
DATA = os.path.abspath(r"release\StageRelay-0.3.1-codex-dsh\Stage Relay\data")
BASE = "http://127.0.0.1:4318"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {str(detail)[:300]}"))


def panel(path, payload=None, cookie=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Relay-Client", "panel")
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r), r.headers.get("Set-Cookie", "").split(";")[0]


def mcp(agent, calls):
    """Speak MCP to the same bridge the member uses."""
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "stage-verify", "version": "1"}}},
                # The server exposes tools only after this notification; without
                # it every call answers with a not-initialized error.
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}]
    for index, (name, args) in enumerate(calls, start=3):
        messages.append({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
    proc = subprocess.run([BRIDGE, "--agent", agent, "--port", "4318", "--data-dir", DATA],
                          input="\n".join(json.dumps(m) for m in messages) + "\n",
                          capture_output=True, text=True, timeout=90,
                          encoding="utf-8", errors="replace")
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


with urllib.request.urlopen(BASE + "/", timeout=20) as page:
    cookie = page.headers.get("Set-Cookie", "").split(";")[0]

# Fixture project at the shared workspace, clearly marked as a fixture.
workspace = os.path.abspath(r"qa\stage-verify-workspace")
os.makedirs(workspace, exist_ok=True)
made, _ = panel("/api/action", {"action": "project_create", "args": {
    "title": "阶段汇总验证（夹具）", "goal": "验证阶段汇总全链路", "workspace": workspace}}, cookie)
pid = made["result"]["id"]
panel("/api/action", {"action": "assign", "args": {
    "project_id": pid, "assignments": {"planner": "codex", "executor": "deepseek",
                                       "reporter": "antigravity"}}}, cookie)
panel("/api/action", {"action": "project_message", "args": {
    "project_id": pid, "body": "【隔离测试夹具】不代表真实用户验收。"}}, cookie)
task, _ = panel("/api/action", {"action": "task_create", "args": {
    "project_id": pid, "title": "阶段板块", "acceptance": "可核对", "owner": "deepseek"}}, cookie)
tid = task["result"]["id"]
panel("/api/action", {"action": "dispatch", "args": {"project_id": pid}}, cookie)
# Only a member can report and review; the panel refuses those on purpose, so the
# bridge does it — which is also what makes this a real end-to-end check.
mcp("deepseek", [("relay_report", {"project_id": pid, "task_id": tid, "status": "review",
                                   "evidence": "夹具检查结果"})])
mcp("codex", [("relay_review", {"project_id": pid, "task_id": tid, "approved": True,
                               "evidence": "夹具独立核验"})])
issue, _ = panel("/api/action", {"action": "issue_create", "args": {
    "project_id": pid, "title": "阶段一议题", "body": "夹具问题",
    "members": ["codex", "deepseek"],
    "options": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}]}}, cookie)
iid = issue["result"]["id"]
panel("/api/action", {"action": "resolve", "args": {
    "project_id": pid, "issue_id": iid, "choice": issue["result"]["options"][0]["id"]}}, cookie)

# The supervisor asks through its own MCP tool: this is the hop that used to die.
replies = mcp("codex", [("relay_request_summary", {"project_id": pid, "issue_id": iid})])
call = next((r for r in replies if r.get("id") == 3), {})
error = call.get("result", {}).get("isError", True)
check("监督经 MCP 请求阶段汇总", not error, call.get("result", {}).get("content"))
stage = {}
if not error:
    stage = json.loads(call["result"]["content"][0]["text"])
    check("阶段带 issue_id 与修订号", stage.get("issue_id") == iid and stage.get("revision") == 1, stage)

if stage.get("id"):
    submit = mcp("antigravity", [("relay_stage_summary", {
        "project_id": pid, "stage_id": stage["id"], "body": "阶段一完成说明"})])
    reply = next((r for r in submit if r.get("id") == 3), {})
    ok = not reply.get("result", {}).get("isError", True)
    check("汇总负责人经 MCP 提交阶段汇总", ok, reply.get("result", {}).get("content"))
    state, _ = panel("/api/state", cookie=cookie)
    target = [p for p in state["projects"] if p["id"] == pid][0]
    stages = target.get("stages", [])
    check("阶段历史已持久化", bool(stages) and stages[0]["status"] == "submitted", stages)
    # Same revision must not be accepted twice.
    again = mcp("antigravity", [("relay_stage_summary", {
        "project_id": pid, "stage_id": stage["id"], "body": "重复提交"})])
    dup = next((r for r in again if r.get("id") == 3), {})
    check("同一修订重复提交被拒", dup.get("result", {}).get("isError") is True, dup.get("result"))

print(json.dumps({"results": results, "fixture_project": pid}, ensure_ascii=False))
