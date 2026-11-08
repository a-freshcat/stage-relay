"""Packaged 0.3.8 check: reference recheck, one-shot claim, per-item restore.

Runs the real exe on isolated data inside qa/ and reproduces the reviewer's
scenario: an attachment previewed as a candidate, then referenced by a message
before execution. The file must survive.
"""

import base64
import json
import os
import subprocess
import time
import http.client
from pathlib import Path

root = Path(__file__).resolve().parent.parent
exe = root / "release/StageRelay-0.3.10-codex-dsh/Stage Relay/Stage Relay.exe"
data = root / "qa/release-0310-data"
data.mkdir(exist_ok=True)
workspace = data / "workspace"
workspace.mkdir(exist_ok=True)
port = 4355
title = f"0.3.8返修检查-{int(time.time())}"

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
    assert health["version"] == "0.3.10-codex-dsh", health

    _, _, headers = request("GET", "/")
    cookie = headers["Set-Cookie"].split(";")[0]
    panel = {"Cookie": cookie, "Content-Type": "application/json", "X-Relay-Client": "panel"}
    created = request("POST", "/api/action", {"action": "project_create", "args": {
        "title": title, "goal": "复核执行时引用检查", "workspace": str(workspace.resolve())}}, panel)
    pid = json.loads(created[1])["result"]["id"]

    scratch = data / "tmp" / pid
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / ".stage-relay-owner.json").write_text(json.dumps({"project": pid}), encoding="utf-8")
    (scratch / "scratch.bin").write_bytes(b"s" * 12)
    uploaded = request("POST", "/api/upload", {"name": "预览后被引用.txt",
                                               "data": base64.b64encode(b"z" * 20).decode(),
                                               "project_id": pid}, panel)
    file_id = json.loads(uploaded[1])["id"]
    old = time.time() - 40 * 86400
    for path in [scratch / ".stage-relay-owner.json", scratch / "scratch.bin",
                 data / "uploads" / file_id, data / "uploads" / f"{file_id}.json"]:
        os.utime(path, (old, old))

    status, body, _ = request("GET", f"/api/cleanup/preview?project={pid}", headers={"Cookie": cookie})
    plan = json.loads(body)
    assert any(Path(i["path"]).name == file_id for i in plan["items"]), plan["items"]

    # Reference the attachment AFTER the user saw the candidate list.
    posted = request("POST", "/api/action", {"action": "project_message",
                                             "args": {"project_id": pid, "body": "现在引用了附件",
                                                      "attachments": [{"id": file_id}]}}, panel)
    assert posted[0] == 200, posted
    closed = request("POST", "/api/project/close", {"project_id": pid, "retention_days": 1}, panel)
    assert closed[0] == 200, closed

    status, body, _ = request("POST", "/api/cleanup/execute",
                              {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    assert status == 200, body
    executed = json.loads(body)
    moved_names = {Path(i["path"]).name for i in executed["moved"]}
    assert file_id not in moved_names, executed
    attachment_exists = (data / "uploads" / file_id).exists()
    assert attachment_exists, "referenced attachment must stay"
    skip_reasons = [i.get("reason", "") for i in executed["skipped"]]

    status, body, _ = request("POST", "/api/cleanup/execute",
                              {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    one_shot_refused = status == 400

    status, body, _ = request("POST", "/api/cleanup/restore",
                              {"project_id": pid, "plan_id": plan["id"], "confirm": True}, panel)
    restored = json.loads(body)

    print(json.dumps({
        "release_version": health["version"],
        "packaged_exe_checked": True,
        "referenced_attachment_kept": attachment_exists,
        "moved": len(executed["moved"]),
        "skipped": len(executed["skipped"]),
        "reference_skip": any("引用了此附件" in r for r in skip_reasons),
        "one_shot_refused": one_shot_refused,
        "restored": len(restored["restored"]),
        "restore_failed": len(restored.get("failed", [])),
    }, ensure_ascii=False))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)