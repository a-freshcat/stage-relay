"""Ask the live panel what its model catalog actually returns per member.

This is the decisive check: the UI can only show a depth selector when the
catalog entry carries `efforts`. So the question is not whether the code exists,
but what the running instance gets back from the native app.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:4318"
DATA = os.path.abspath(r"release\StageRelay-0.3.1-codex-dsh\Stage Relay\data")


def panel(path, payload=None, cookie=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Relay-Client", "panel")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r), r.headers.get("Set-Cookie", "").split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), ""


with urllib.request.urlopen(BASE + "/", timeout=20) as page:
    cookie = page.headers.get("Set-Cookie", "").split(";")[0]
status, state, _ = panel("/api/state", cookie=cookie)
project = [p for p in state["projects"] if "Stage Relay" in p["title"]][0]["id"]
print("project", project)
print("codex native:", json.dumps(state.get("codexNative"), ensure_ascii=False)[:500])
print("bindings:", [(b["agent"], b["session"][:22]) for b in state["runtime"]["bindings"]])

for agent in ("codex", "deepseek"):
    status, queued, _ = panel("/api/runtime/model", {
        "project_id": project, "agent_id": agent, "operation": "model-catalog"}, cookie)
    if status != 200:
        print(agent, "queue failed", status, queued)
        continue
    job_id = queued["id"]
    delivered = None
    for _ in range(30):
        time.sleep(2)
        _, current, _ = panel("/api/state", cookie=cookie)
        found = [j for j in current["runtime"]["jobs"] if j["id"] == job_id]
        if found and found[0]["status"] in ("delivered", "failed"):
            delivered = found[0]
            break
    print("=" * 20, agent, delivered["status"] if delivered else "timeout")
    if delivered:
        receipt = delivered["detail"].get("receipt")
        print("  receipt:", str(receipt)[:200])
        models = (delivered["detail"].get("result") or {}).get("models") or []
        if not models and isinstance(receipt, str):
            try:
                models = json.loads(receipt).get("models", [])
            except ValueError:
                pass
        print("  models:", len(models))
        with_efforts = [m for m in models if m.get("efforts")]
        print("  带 efforts 的模型:", len(with_efforts))
        for m in models[:3]:
            print("   ", json.dumps(m, ensure_ascii=False)[:220])
