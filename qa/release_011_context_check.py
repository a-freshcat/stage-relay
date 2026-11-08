"""Packaged 0.1.1 check: compact wake, incremental inbox, on-demand reads.

The previous script proves the cleanup paths still work. This one proves the
context work survives packaging: the delivered prompt stays small on a grown
project, relay_inbox continues from a cursor, and the new read actions return
the complete evidence through the packaged bridge.
"""
import base64
import http.client
import json
import os
import subprocess
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
exe = root / "release/StageRelay-0.1.1/Stage Relay/Stage Relay.exe"
data = root / "qa/release-011-ctx-data"
data.mkdir(exist_ok=True)
workspace = data / "workspace"
workspace.mkdir(exist_ok=True)
port = 4361

startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 0
process = subprocess.Popen(
    [str(exe), "--port", str(port), "--data-dir", str(data), "--app-home", str(data / "fake-home")],
    startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
)


def request(method, path, value=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(value).encode() if value is not None else None
    connection.request(method, path, body, headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, payload, dict(response.getheaders()))
    connection.close()
    return result


EVIDENCE = "检查命令 python -m unittest discover -s tests -q 通过；" * 40


def main():
    until = time.monotonic() + 30
    while True:
        try:
            health = json.loads(request("GET", "/api/health")[1])
            break
        except OSError:
            if time.monotonic() > until:
                raise
            time.sleep(0.25)
    assert health["version"] == "0.1.1", health

    _, _, headers = request("GET", "/")
    cookie = headers["Set-Cookie"].split(";")[0]
    panel = {"Cookie": cookie, "Content-Type": "application/json", "X-Relay-Client": "panel"}
    created = request("POST", "/api/action", {"action": "project_create", "args": {
        "title": "打包上下文检查", "goal": "验证打包后唤醒与按需读取",
        "workspace": str(workspace.resolve())}}, panel)
    pid = json.loads(created[1])["result"]["id"]

    task = json.loads(request("POST", "/api/action", {"action": "task_create", "args": {
        "project_id": pid, "title": "板块", "acceptance": "命令通过", "owner": "deepseek"}}, panel)[1])["result"]
    request("POST", "/api/action", {"action": "dispatch", "args": {"project_id": pid}}, panel)
    executor = {"Authorization": "Bearer " + token_for(panel, "deepseek"),
                "Content-Type": "application/json"}
    planner = {"Authorization": "Bearer " + token_for(panel, "codex"),
               "Content-Type": "application/json"}
    for n in range(6):
        request("POST", "/bridge", {"action": "report", "args": {
            "project_id": pid, "task_id": task["id"], "status": "review",
            "evidence": f"{n}:{EVIDENCE}"}}, executor)
        request("POST", "/bridge", {"action": "review", "args": {
            "project_id": pid, "task_id": task["id"], "approved": True,
            "evidence": f"核验 {n}：{EVIDENCE}"}}, planner)
    request("POST", "/api/action", {"action": "project_message",
                                    "args": {"project_id": pid, "body": "打包后群聊一句"}}, panel)

    bridge = executor
    first = json.loads(request("POST", "/bridge", {"action": "inbox", "args": {}}, bridge)[1])["result"]
    second = json.loads(request("POST", "/bridge", {"action": "inbox", "args": {}}, bridge)[1])["result"]
    everything = json.loads(request("POST", "/bridge", {"action": "inbox", "args": {"full": True}},
                                    bridge)[1])["result"]
    project = next(p for p in first["projects"] if p["id"] == pid)
    summary_bytes = len(json.dumps(project, ensure_ascii=False).encode())
    full = json.loads(request("POST", "/bridge", {"action": "read_project",
                                                  "args": {"project_id": pid}}, bridge)[1])["result"]
    task_full = json.loads(request("POST", "/bridge", {"action": "read_task",
                                                       "args": {"project_id": pid,
                                                                "task_id": task["id"]}}, bridge)[1])["result"]
    chat_id = json.loads(request("POST", "/bridge", {"action": "read_project",
                                                     "args": {"project_id": pid}}, bridge)[1])["result"]["chat"][0]["id"]
    message = json.loads(request("POST", "/bridge", {"action": "read_message",
                                                     "args": {"project_id": pid,
                                                              "message_id": chat_id}}, bridge)[1])["result"]
    rules = json.loads(request("POST", "/bridge", {"action": "rules", "args": {}}, bridge)[1])["result"]
    print(json.dumps({
        "release_version": health["version"],
        "packaged_exe_checked": True,
        "inbox_project_bytes": summary_bytes,
        "first_read_events": len(first["events"]),
        "second_read_events": len(second["events"]),
        "full_history_events": len(everything["events"]),
        "incremental_default": len(second["events"]) < len(first["events"]),
        "page_next_after": everything["page"]["next_after"],
        "summary_omits_evidence": EVIDENCE.strip() not in json.dumps(project, ensure_ascii=False),
        "full_read_tasks": len(full["tasks"]),
        "full_task_has_evidence": EVIDENCE.strip() in task_full["evidence"],
        "message_read": message["body"],
        "rules_version": rules["version"],
        "rules_bytes": len(rules["text"].encode()),
    }, ensure_ascii=False))


def token_for(panel, agent):
    credentials = json.loads((data / "bridge-credentials.json").read_text(encoding="utf-8"))
    return credentials[agent]


try:
    main()
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)

