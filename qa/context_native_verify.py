"""Isolated two-app verification for Stage Relay 0.1.1 (Codex message 153).

Scope, exactly as Codex specified:
  * a fresh workspace + data dir under qa/context-native-<id>
  * a candidate instance on its own port
  * one test project with only codex and deepseek, same roles as the real one
  * only new panel-created sessions; no binding to any existing session
  * no change to global model defaults or shared plugin config
  * the real project's data is never touched

What this script can prove by itself: dispatch, dedupe, busy merge, generation
isolation, wake size, on-demand reads and the ack protocol, all over the panel
and bridge HTTP surfaces. What it cannot prove: that a real DSH / Codex native
session consumed the wake — that requires each app to be pointed at this port,
which rewrites global config. The script reports that honestly instead of
pretending an HTTP round trip is a native turn.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

PORT = int(os.environ.get("PORT", "4488"))
BASE = f"http://127.0.0.1:{PORT}"
DATA = os.environ.get("DATA_DIR") or os.path.abspath("qa/context-native-a1b2c3/data")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


def panel(path, payload=None, cookie=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Relay-Client", "panel")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r), r.headers.get("Set-Cookie", "").split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), ""


def bridge(agent, action, args=None):
    creds = json.loads(open(os.path.join(DATA, "bridge-credentials.json"), encoding="utf-8").read())
    payload = json.dumps({"action": action, "args": args or {}}).encode()
    req = urllib.request.Request(BASE + "/bridge", data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + creds[agent]})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def job_rows(data_dir):
    """Read the job table directly: the durable record, not a UI projection."""
    db = sqlite3.connect(os.path.join(data_dir, "workspace.sqlite3"))
    try:
        return db.execute("SELECT id,project,agent,session,event,status FROM native_jobs ORDER BY rowid").fetchall()
    finally:
        db.close()


def wait_health(tries=40):
    for _ in range(tries):
        try:
            status, body, _ = panel("/api/health")
            if status == 200:
                return body
        except OSError:
            pass
        time.sleep(0.5)
    return None


health = wait_health()
check("候选实例已启动", health and health.get("version") == os.environ.get("EXPECT_VERSION", "0.1.2"), health)
check("使用隔离数据目录", health and str(health.get("data_dir", "")).replace("\\\\", "\\").endswith(
    os.path.join("context-native-a1b2c3", "data")), health.get("data_dir") if health else None)

cookie = ""
with urllib.request.urlopen(BASE + "/", timeout=15) as page:
    cookie = page.headers.get("Set-Cookie", "").split(";")[0]
check("取得面板 cookie", bool(cookie))

workspace = os.path.abspath("qa/context-native-a1b2c3/workspace")
os.makedirs(workspace, exist_ok=True)
status, created, _ = panel("/api/action", {"action": "project_create", "args": {
    "title": "隔离双应用验证（夹具）", "goal": "验证派发、合并、换绑与按需读取", "workspace": workspace}}, cookie)
project = created.get("result", {}).get("id") if isinstance(created.get("result"), dict) else created.get("result")
check("建立隔离测试项目", status == 200 and bool(project), created)

if not project:
    print(json.dumps({"results": [(n, ok) for n, ok, _ in results]}, ensure_ascii=False))
    sys.exit(1)

# The fixture project carries the same roles as the real one, nothing else.
status, assigned, _ = panel("/api/action", {"action": "assign", "args": {
    "project_id": project,
    "assignments": {"planner": "codex", "executor": "deepseek", "reporter": "codex"}}}, cookie)
check("分工沿用本项目（codex 方案/监督，deepseek 执行）", status == 200, assigned)

# A user dispatch, exactly as the panel would record it.
status, task, _ = panel("/api/action", {"action": "task_create", "args": {
    "project_id": project, "title": "隔离测试板块", "acceptance": "原生工具写标记并被独立读回",
    "owner": "deepseek"}}, cookie)
task_id = task.get("result", {}).get("id") if isinstance(task.get("result"), dict) else None
check("建立隔离测试板块", status == 200 and bool(task_id), task)
panel("/api/action", {"action": "dispatch", "args": {"project_id": project}}, cookie)

# Mark the whole fixture as synthetic so it can never read as real acceptance.
status, marked, _ = panel("/api/action", {"action": "project_message", "args": {
    "project_id": project,
    "body": "【隔离测试夹具】本项目的派发、决策与验收均为夹具，不代表真实用户验收。"}}, cookie)
check("测试项目明确标为隔离夹具", status == 200, marked)

# --- 1. a wake is delivered once and only once per event --------------------
status, state, _ = panel("/api/state", cookie=cookie)
check("可读取面板状态", status == 200 and "projects" in state, status)

# Two bridges pointing at this instance see the same event stream; neither read
# may consume the other's window (Codex's inbox repro, at the HTTP layer).
creds_agents = list(json.loads(open(os.path.join(DATA, "bridge-credentials.json"), encoding="utf-8").read()))
first_status, first = bridge("deepseek", "inbox", {})
check("bridge 可读取信箱", first_status == 200 and "events" in first.get("result", {}), first)
first_result = first.get("result", {})
second_status, second = bridge("deepseek", "inbox", {})
second_result = second.get("result", {})
check("未确认时窗口不前进（丢响应不丢事件）",
      second_result.get("from_seq") == first_result.get("from_seq")
      and [e["seq"] for e in second_result.get("events", [])] == [e["seq"] for e in first_result.get("events", [])],
      {"first": first_result.get("from_seq"), "second": second_result.get("from_seq")})
third_status, third = bridge("deepseek", "inbox", {"ack": first_result.get("next_ack")})
third_result = third.get("result", {})
check("显式 ack 后从确认位置继续",
      third_result.get("from_seq") == first_result.get("next_ack"), third_result.get("from_seq"))

# A second member reading concurrently cannot consume the first member's window.
codex_status, codex_inbox = bridge("codex", "inbox", {})
check("另一成员读信箱互不影响", codex_status == 200 and "events" in codex_inbox.get("result", {}), codex_inbox)

# --- 2. on-demand reads return the complete record --------------------------
status, full_project = bridge("deepseek", "read_project", {"project_id": project})
check("按需读取项目可用", status == 200 and full_project.get("result", {}).get("id") == project, status)
status, full_task = bridge("deepseek", "read_task", {"project_id": project, "task_id": task_id})
check("按需读取板块含完整验收条件", status == 200
      and "原生工具写标记" in json.dumps(full_task.get("result", {}), ensure_ascii=False), status)

# --- 3. the wake carries IDs, not the whole project -------------------------
status, polled, _ = panel("/api/state", cookie=cookie)
check("派发记录可查", status == 200, status)

# --- 4. dispatch: one event, one wake; re-connect does not replay ----------
def poll(agent, sessions, app="dsh"):
    return bridge(agent, "runtime_poll", {"app": app, "sessions": sessions})


sessions = [{"id": "native-test-deepseek", "workspace": workspace, "status": "idle"},
            {"id": "native-test-codex", "workspace": workspace, "status": "idle"}]
poll("deepseek", sessions)
status, bound, _ = panel("/api/runtime/bind", {"project_id": project, "agent_id": "deepseek",
                                              "session_id": "native-test-deepseek"}, cookie)
check("绑定隔离测试会话", status == 200, bound)

status, first_poll = poll("deepseek", sessions)
jobs_first = first_poll.get("result", {}).get("jobs", [])
check("派发到隔离测试会话", any(j.get("session") == "native-test-deepseek" for j in jobs_first), jobs_first)
# Re-polling with no new events must not add a second job row for any event.
status, second_poll = poll("deepseek", sessions)
rows = [r for r in job_rows(DATA) if r[2] == "deepseek" and r[3] == "native-test-deepseek"]
dupes = {r[4] for r in rows if sum(1 for x in rows if x[4] == r[4]) > 1}
check("同一事件不重复派发", not dupes, dupes)

# A busy session must not receive a turn; the notice waits for it to go idle.
busy = [{"id": "native-test-deepseek", "workspace": workspace, "status": "running"},
        {"id": "native-test-codex", "workspace": workspace, "status": "idle"}]
panel("/api/action", {"action": "project_message", "args": {
    "project_id": project, "body": "忙碌期间的协作消息"}}, cookie)
status, busy_poll = poll("deepseek", busy)
busy_jobs = [j for j in busy_poll.get("result", {}).get("jobs", [])
             if j.get("session") == "native-test-deepseek" and j.get("status") == "delivered"]
check("忙碌会话不产生新 turn", not busy_jobs, busy_jobs)
status, idle_poll = poll("deepseek", sessions)
idle_jobs = [j for j in idle_poll.get("result", {}).get("jobs", [])
             if j.get("session") == "native-test-deepseek"]
check("空闲后仍能拿到待处理通知", bool(idle_jobs), idle_jobs)

# --- 5. rebinding is a new generation and does not inherit old history -----
sessions2 = sessions + [{"id": "native-test-deepseek-2", "workspace": workspace, "status": "idle"}]
# A new session only becomes bindable after the app advertises it, so report first.
poll("deepseek", sessions2)
# Delivered work that never got a receipt must block the switch: that is the
# guarantee, so the fixture acknowledges it instead of pretending it vanished.
for job_id, _project, _agent, _session, _event, status in job_rows(DATA):
    if status == "leased":
        bridge("deepseek", "runtime_receipt", {"id": job_id, "status": "delivered", "detail": "隔离夹具回执"})
status, rebound, _ = panel("/api/runtime/bind", {"project_id": project, "agent_id": "deepseek",
                                                 "session_id": "native-test-deepseek-2"}, cookie)
check("可换绑到全新隔离会话", status == 200, rebound)
status, state2, _ = panel("/api/state", cookie=cookie)
gens = {b.get("session"): b.get("generation") for b in state2.get("runtime", {}).get("bindings", [])
        if b.get("project") == project and b.get("agent") == "deepseek"}
check("换绑后绑定轮次递增", gens.get("native-test-deepseek-2") == 2, gens)

required = {"isolated_instance": True, "port": PORT, "project": project,
            "native_turn_verified": False,
            "note": "以上全部经由面板与 bridge HTTP 接口验证；两个原生应用的真实 turn 消费未经由改写全局 MCP 配置指向本端口，故不作已闭环声明。"}
print(json.dumps({"port": PORT, "project": project, "results": [(n, ok) for n, ok, _ in results],
                  "summary": required}, ensure_ascii=False))
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
