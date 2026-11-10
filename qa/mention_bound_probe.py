"""Isolated reproduction: the fourth member message must wake its addressee."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.domain import Store
from relay.runtime import Runtime

root = Path(tempfile.mkdtemp(prefix="mention-bound-", dir=Path(__file__).resolve().parent))
store = Store(root / "state.sqlite3")
runtime = Runtime(store)
project = store.apply("project_create", {"title": "mention limit probe", "goal": "fourth message", "workspace": str(root)})
pid = project["id"]
sessions = [{"id": "probe-codex", "workspace": str(root), "status": "idle"}]
payload = {"app": "codex", "sessions": sessions}
runtime.poll("codex", payload)
runtime.bind(pid, "codex", "probe-codex")

def drain():
    result = []
    while True:
        jobs = runtime.poll("codex", payload)["jobs"]
        if not jobs:
            return result
        result.extend(jobs)
        for job in jobs:
            runtime.receipt("codex", {"id": job["id"], "status": "delivered"})

issue = store.apply("issue_create", {"project_id": pid, "title": "native handoff", "body": "@deepseek initial plan", "members": ["codex", "deepseek"]}, "codex")
for n in range(2):
    store.apply("message", {"project_id": pid, "issue_id": issue["id"], "body": f"@deepseek clarification {n}"}, "codex")
drain()
store.apply("message", {"project_id": pid, "issue_id": issue["id"], "body": "@codex ready-for-supervision"}, "deepseek")
before = drain()
with store.connect() as db:
    deferred = db.execute("SELECT seq,reason FROM native_deferred WHERE agent='codex'").fetchall()
store.apply("message", {"project_id": pid, "issue_id": issue["id"], "body": "@codex why no reply?"})
after = drain()
received_before = any("ready-for-supervision" in j["prompt"] for j in before)
received_after = any("ready-for-supervision" in j["prompt"] for j in after)
print(json.dumps({"data": str(root), "received_before_user": received_before, "deferred_before_user": deferred, "received_after_user": received_after}))
if not received_before:
    print("FAIL: explicitly addressed handoff waits for a further user message")
    sys.exit(1)
print("PASS: explicitly addressed handoff delivered without user intervention")
