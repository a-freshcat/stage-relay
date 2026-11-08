"""Exercise the packaged 0.3.3 GUI release with isolated data.

Starts the real exe, then walks the new close + cleanup flow over HTTP so the
packaged build - not just the source tree - is proven to serve 0.3.3 behaviour.
Nothing outside qa/ is written and no real data directory is opened.
"""

import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
exe = (root / "release/StageRelay-0.3.3-codex-dsh/Stage Relay/Stage Relay.exe").resolve()
assert exe.is_relative_to((root / "release").resolve()) and exe.is_file(), exe
data = root / "qa/release-033-data"
data.mkdir(exist_ok=True)
workspace = data / "workspace"
workspace.mkdir(exist_ok=True)
port = 4345

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
    until = time.monotonic() + 25
    while True:
        try:
            status, body, _ = request("GET", "/api/health")
            health = json.loads(body)
            break
        except OSError:
            if time.monotonic() > until:
                raise
            time.sleep(0.25)
    assert health["version"] == "0.3.3-codex-dsh", health

    _, _, headers = request("GET", "/")
    cookie = headers["Set-Cookie"].split(";")[0]
    panel = {"Cookie": cookie, "Content-Type": "application/json", "X-Relay-Client": "panel"}
    status, body, _ = request("GET", "/api/state", headers={"Cookie": cookie})
    assert status == 200, status
    project = json.loads(body)["projects"][0]

    status, body, _ = request(
        "POST", "/api/action",
        {"action": "project_create", "args": {"title": "打包检查", "goal": "验证结束与清理接口", "workspace": str(workspace.resolve())}},
        panel,
    )
    assert status == 200, body
    project = json.loads(body)["result"]

    status, body, _ = request(
        "POST", "/api/action",
        {"action": "project_message", "args": {"project_id": project["id"], "body": "打包版项目群聊"}},
        panel,
    )
    assert status == 200, body

    status, body, _ = request(
        "POST", "/api/project/close",
        {"project_id": project["id"], "retention_days": 3},
        panel,
    )
    assert status == 200, body
    closed = json.loads(body)
    assert closed["result"]["closed"] is True

    status, body, _ = request(
        "GET", f"/api/cleanup/preview?project={project['id']}",
        headers={"Cookie": cookie},
    )
    assert status == 200, body
    preview = json.loads(body)

    status, body, _ = request(
        "POST", "/api/cleanup/execute",
        {"project_id": project["id"], "confirm": True},
        panel,
    )
    assert status == 200, body

    print(json.dumps({
        "release_version": health["version"],
        "packaged_exe_checked": True,
        "project_closed": closed["result"]["closed"],
        "chat_messages": len(closed["result"]["chat"]),
        "planned_archive_or_unbind": len(closed["plan"]["archived"]) + len(closed["plan"]["unbound"]),
        "preview_items": len(preview["items"]),
        "preview_retained": len(preview["retained"]),
    }, ensure_ascii=False))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
