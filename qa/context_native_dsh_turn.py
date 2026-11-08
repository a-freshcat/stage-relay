"""Bind the isolated DSH native session and drive one real turn through it.

The DSH plugin running inside the isolated profile polls the candidate instance on
4488 once a second. That is a real native polling loop, so a wake queued for its
session will be consumed by DSH itself via `agent.followup()` — not by an API call
from this script. This script only sets the fixture up and reads the outcome.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4488"
DATA = os.path.abspath(r"qa\context-native-a1b2c3\data")


def panel(path, payload=None, cookie=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Relay-Client", "panel")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.load(r), r.headers.get("Set-Cookie", "").split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), ""


def db():
    return sqlite3.connect(os.path.join(DATA, "workspace.sqlite3"))


with urllib.request.urlopen(BASE + "/", timeout=20) as page:
    cookie = page.headers.get("Set-Cookie", "").split(";")[0]
_, state, _ = panel("/api/state", cookie=cookie)
project = [p for p in state["projects"] if "隔离" in p["title"]][0]["id"]

handle = db()
row = handle.execute("SELECT body FROM native_hosts WHERE agent='deepseek'").fetchone()
sessions = json.loads(row[0]).get("sessions", []) if row else []
native = [s for s in sessions if s.get("status")]
print("native sessions reported by DSH:",
      [(s.get("id", "")[:26], s.get("status"), s.get("workspace")) for s in native])
if not native:
    print("DSH has not advertised a session yet")
    sys.exit(1)
session_id = native[0]["id"]
native_workspace = native[0].get("workspace")

# A project's workspace is fixed at creation (changing it would mix delivery
# evidence), and a session whose workspace differs is refused by design. So the
# fixture is created *at* the native session's own workspace instead.
status, made, _ = panel("/api/action", {"action": "project_create", "args": {
    "title": "DSH 原生会话隔离验证（夹具）",
    "goal": "验证真实 DSH 原生轮次消费 Stage Relay 唤醒",
    "workspace": native_workspace}}, cookie)
project = made["result"]["id"] if status == 200 else project
print("project", project, "workspace", native_workspace)
panel("/api/action", {"action": "assign", "args": {
    "project_id": project,
    "assignments": {"planner": "codex", "executor": "deepseek", "reporter": "codex"}}}, cookie)
panel("/api/action", {"action": "project_message", "args": {
    "project_id": project,
    "body": "【隔离测试夹具】本项目仅用于验证真实 DSH 原生轮次，不代表真实用户验收。"}}, cookie)

handle = db()
handle.execute("DELETE FROM native_bindings WHERE project=? AND agent='deepseek'", (project,))
handle.commit()
handle.close()

status, bound, _ = panel("/api/runtime/bind", {"project_id": project, "agent_id": "deepseek",
                                              "session_id": session_id}, cookie)
print("bind", status, bound)

# A user instruction the native session must act on.
status, created, _ = panel("/api/action", {"action": "project_message", "args": {
    "project_id": project,
    "body": "【隔离测试】请用你自己的 Stage Relay 工具读一次 relay_inbox，"
            "并在回复里写出 from_seq、next_ack 与事件条数。不要修改任何真实项目。"}}, cookie)
print("message", status)

# Wait for the native plugin's own poll to pick it up and the harness to answer.
deadline = time.time() + 180
seen = None
while time.time() < deadline:
    handle = db()
    jobs = handle.execute(
        "SELECT id,event,status FROM native_jobs WHERE agent='deepseek' AND session=? "
        "ORDER BY rowid DESC LIMIT 6", (session_id,)).fetchall()
    activity = handle.execute(
        "SELECT seq,body FROM native_activity WHERE agent='deepseek' AND session=? "
        "ORDER BY seq DESC LIMIT 4", (session_id,)).fetchall()
    handle.close()
    if jobs != seen:
        print("jobs", jobs)
        seen = jobs
    if activity:
        print("activity", [(a[0], json.loads(a[1]).get("kind")) for a in activity])
        break
    time.sleep(5)

handle = db()
final = handle.execute("SELECT id,event,status FROM native_jobs WHERE agent='deepseek' "
                       "AND session=? ORDER BY rowid", (session_id,)).fetchall()
acts = handle.execute("SELECT seq,body FROM native_activity WHERE agent='deepseek' "
                      "AND session=? ORDER BY seq", (session_id,)).fetchall()
handle.close()
print("final jobs", final)
print("activity count", len(acts))
for seq, body in acts:
    detail = json.loads(body)
    text = (detail.get("text") or detail.get("delta") or "")[:300]
    print("  ", seq, detail.get("kind"), detail.get("role"), text.replace("\n", " "))
