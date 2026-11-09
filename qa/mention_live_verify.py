"""Verify mention routing on the live panel: @one member must not wake the rest."""
import json
import os
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4318"


def panel(path, payload=None, cookie=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Relay-Client", "panel")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.load(r), r.headers.get("Set-Cookie", "").split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), ""


with urllib.request.urlopen(BASE + "/", timeout=20) as page:
    cookie = page.headers.get("Set-Cookie", "").split(";")[0]
status, health, _ = panel("/api/health")
print("panel", health.get("version"))

# A throwaway fixture, so the real project's chat stays untouched.
workspace = os.path.abspath(r"qa\mention-workspace")
os.makedirs(workspace, exist_ok=True)
_, made, _ = panel("/api/action", {"action": "project_create", "args": {
    "title": "@ 路由验证（夹具）", "goal": "验证定向通知", "workspace": workspace}}, cookie)
project = made["result"]["id"]
panel("/api/action", {"action": "assign", "args": {
    "project_id": project,
    "assignments": {"planner": "codex", "executor": "deepseek", "reporter": "codex"}}}, cookie)
panel("/api/action", {"action": "project_message", "args": {
    "project_id": project, "body": "【隔离测试夹具】占位"}}, cookie)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


# The user's exact scenario: @Codex only.
status, _, _ = panel("/api/action", {"action": "project_message", "args": {
    "project_id": project, "body": "@codex 只给这一位", "mentions": ["codex"]}}, cookie)
check("@ 单个成员消息可发送", status == 200, status)

status, project_state, _ = panel("/api/state", cookie=cookie)
target = [p for p in project_state["projects"] if p["id"] == project][0]
check("夹具项目已建立", bool(target))

# Read the recipient list the server actually recorded.
import sqlite3
DATA = os.path.abspath(r"release\StageRelay-0.3.1-codex-dsh\Stage Relay\data")
db = sqlite3.connect(os.path.join(DATA, "workspace.sqlite3"))
rows = db.execute("SELECT body FROM events WHERE project=? AND kind='project_message' "
                  "ORDER BY seq DESC LIMIT 2", (project,)).fetchall()
db.close()
details = [json.loads(r[0]) for r in rows]
targeted = [d for d in details if d.get("recipients") == ["codex"]]
check("recpients 只含被 @ 的成员", bool(targeted), details)
check("事件记录 mentions", any(d.get("mentions") == ["codex"] for d in details), details)

# An unknown mention must be refused, not broadcast.
status, refused, _ = panel("/api/action", {"action": "project_message", "args": {
    "project_id": project, "body": "@nobody 这是谁"}}, cookie)
check("未知 @ 被拒绝而非广播", status != 200, (status, refused))

print(json.dumps({"results": results}, ensure_ascii=False))
