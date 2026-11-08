"""Packaged 0.3.7 check: frozen cleanup plan, one-shot execution, recovery.

Creates its own project each run (unique title) inside qa/ so repeated runs never
reuse a stale project or touch real data.
"""

import base64
import json
import os
import subprocess
import time
import http.client
from pathlib import Path

root = Path(__file__).resolve().parent.parent
exe = root / "release/StageRelay-0.3.7-codex-dsh/Stage Relay/Stage Relay.exe"
data = root / "qa/release-037-cleanup-data"
data.mkdir(exist_ok=True)
workspace = data / "workspace"
workspace.mkdir(exist_ok=True)
port = 4351
title = f"冻结清理检查-{int(time.time())}"

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

    created = request("POST", "/api/action", {"action": "project_create", "args": {
        "title": title, "goal": "验证冻结清理与恢复", "workspace": str(workspace.resolve())}}, panel)
    assert created[0] == 200, created
    pid = json.loads(created[1])["result"]["id"]

    scratch = data / "tmp" / pid
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / ".stage-relay-owner.json").write_text(json.dumps({"project": pid}), encoding="utf-8")
    (scratch / "work.bin").write_bytes(b"w" * 16)
    uploaded = request("POST", "/api/upload", {"name": "归属孤儿.txt",
                                               "data": base64.b64encode(b"y" * 24).decode(),
                                               "project_id": pid}, panel)
    assert uploaded[0] == 200, uploaded
    file_id = json.loads(uploaded[1])["id"]
    old = time.time() - 40 * 86400
    for path in [scratch / ".stage-relay-owner.json", scratch / "work.bin",
                 data / "uploads" / file_id, data / "uploads" / f"{file_id}.json"]:
        os.utime(path, (old, old))

    closed = request("POST", "/api/project/close", {"project_id": pid, "retention_days": 1}, panel)
    assert closed[0] == 200, closed

    status, body, _ = request("GET", f"/api/cleanup/preview?project={pid}", headers={"Cookie": cookie})
    assert status == 200, body
    plan = json.loads(body)
    kinds = sorted({item["kind"] for item in plan["items"]})
    assert "项目临时文件" in kinds and "本项目孤儿附件" in kinds, kinds
    workspace_row = next(r for r in plan["retained"] if r["path"] == str(workspace.resolve()))
    assert workspace_row["size"] is None, workspace_row

    status, body, _ = request("POST", "/api/cleanup/execute",
                              {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    assert status == 200, body
    executed = json.loads(body)
    assert executed["moved"], executed

    status, body, _ = request("POST", "/api/cleanup/execute",
                              {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    assert status == 400, (status, body)

    status, body, _ = request("GET", f"/api/cleanup/recoveries?project={pid}", headers={"Cookie": cookie})
    listed = json.loads(body)
    assert len(listed) == 1, listed

    status, body, _ = request("POST", "/api/cleanup/restore",
                              {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    assert status == 200, body
    restored = json.loads(body)
    assert restored["restored"], restored
    assert all(Path(item["path"]).exists() for item in plan["items"])

    print(json.dumps({
        "release_version": health["version"],
        "packaged_exe_checked": True,
        "kinds": kinds,
        "candidates": len(plan["items"]),
        "moved": len(executed["moved"]),
        "recoveries": len(listed),
        "restored": len(restored["restored"]),
        "workspace_not_scanned": workspace_row["size"] is None,
        "one_shot_plan": True,
    }, ensure_ascii=False))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)