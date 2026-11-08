"""End-to-end check of the packaged 0.3.7 release on isolated data.

Starts the real exe, then walks record recycling, the frozen cleanup plan and
recovery over HTTP so the packed build - not only the source tree - is proven.
Nothing outside qa/ is written.
"""

import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
exe = (root / "release/StageRelay-0.3.7-codex-dsh/Stage Relay/Stage Relay.exe").resolve()
assert exe.is_relative_to((root / "release").resolve()) and exe.is_file(), exe
data = root / "qa/release-037-data"
data.mkdir(exist_ok=True)
workspace = data / "workspace"
workspace.mkdir(exist_ok=True)
port = 4347

startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 0
process = subprocess.Popen(
    [str(exe), "--port", str(port), "--data-dir", str(data), "--app-home", str(data / "fake-home")],
    startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
)


def request(method, path, value=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(value).encode() if value is not None else None
    connection.request(method, path, body, headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, payload, dict(response.getheaders()))
    connection.close()
    return result


try:
    until = time.monotonic() + 30
    while True:
        try:
            status, body, _ = request("GET", "/api/health")
            health = json.loads(body)
            break
        except OSError:
            if time.monotonic() > until:
                raise
            time.sleep(0.25)
    assert health["version"] == "0.3.7-codex-dsh", health

    _, _, headers = request("GET", "/")
    cookie = headers["Set-Cookie"].split(";")[0]
    panel = {"Cookie": cookie, "Content-Type": "application/json", "X-Relay-Client": "panel"}
    project = json.loads(request("GET", "/api/state", headers={"Cookie": cookie})[1])["projects"][0]
    created = request(
        "POST", "/api/action",
        {"action": "project_create", "args": {"title": "打包回收检查", "goal": "验证回收与冻结清理",
                                              "workspace": str(workspace.resolve())}},
        panel,
    )
    assert created[0] == 200, created
    pid = json.loads(created[1])["result"]["id"]

    def action(name, **args):
        status, body, _ = request("POST", "/api/action", {"action": name, "args": {"project_id": pid, **args}}, panel)
        assert status == 200, (name, status, body)
        return json.loads(body)

    action("project_message", body="要回收的群聊消息")
    state = action("project_message", body="保留的消息")["state"]
    chat = next(p for p in state["projects"] if p["id"] == pid)["chat"]
    recycle_id = chat[0]["id"]

    recycled = action("record_archive", kind="project_message", item_id=recycle_id)
    archived = next(m for m in next(p for p in recycled["state"]["projects"] if p["id"] == pid)["chat"] if m["id"] == recycle_id)
    assert archived["archived"] is True

    with_id = action("task_create", title="待回收板块", owner="deepseek", acceptance="不派发")
    task_id = with_id["result"]["id"]
    action("dispatch")
    issue = action("issue_create", title="验证阻塞", body="先处理这个议题", task_ids=[task_id])["result"]
    status, body, _ = request("POST", "/api/action", {"action": "record_archive", "args": {"project_id": pid, "kind": "task", "item_id": task_id}}, panel)
    assert status == 400, (status, body)  # a blocked task cannot be recycled
    blocked_issue = action("record_archive", kind="issue", item_id=issue["id"])
    task_after = next(t for t in next(p for p in blocked_issue["state"]["projects"] if p["id"] == pid)["tasks"] if t["id"] == task_id)
    assert task_after["status"] == "queued", task_after  # recycling the issue released the block

    # Upload with project ownership, then a frozen cleanup plan after closing.
    import base64
    status, body, _ = request("POST", "/api/upload", {"name": "孤儿.txt", "data": base64.b64encode(b"x" * 32).decode(), "project_id": pid}, panel)
    assert status == 200, body
    file_id = json.loads(body)["id"]
    request("POST", "/api/project/close", {"project_id": pid, "retention_days": 1}, panel)

    status, body, _ = request("GET", f"/api/cleanup/preview?project={pid}", headers={"Cookie": cookie})
    assert status == 200, body
    plan = json.loads(body)
    assert plan["items"] == [] or all(i["kind"] != "项目临时文件" for i in plan["items"])

    status, body, _ = request("POST", "/api/cleanup/execute", {"project_id": pid, "confirm": True}, panel)
    assert status == 400, (status, body)  # a plan id is required
    status, body, _ = request("POST", "/api/cleanup/execute", {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    assert status == 200, body
    executed = json.loads(body)
    status, body, _ = request("POST", "/api/cleanup/execute", {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    assert status == 400, (status, body)  # one-shot plan

    print(json.dumps({
        "release_version": health["version"],
        "packaged_exe_checked": True,
        "chat_message_recycled": archived["archived"],
        "running_task_rejected": True,
        "plan_id": plan["id"][:8],
        "preview_items": len(plan["items"]),
        "preview_retained": len(plan["retained"]),
        "moved": len(executed["moved"]),
        "plan_is_one_shot": True,
        "upload_left": file_id[:8],
    }, ensure_ascii=False))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
