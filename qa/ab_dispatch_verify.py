"""A/B isolation check for the agreed dispatch semantics.

Covers: one logical dispatch per project+member+event, reconnect never re-wakes,
pending notifications coalesce into one turn with every event id kept, a switch
never leaks old work into the new session, and the execution record carries
session/generation/job ids.

Everything runs in a temp directory under qa/; no real project or session is used.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.domain import Store
from relay.runtime import Runtime

root = Path(__file__).resolve().parent / "ab-verify"
root.mkdir(exist_ok=True)
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


temp = tempfile.TemporaryDirectory(prefix="ab-", dir=root)
data = Path(temp.name)
store = Store(data / "workspace.sqlite3")
workspace = data / "ws"
workspace.mkdir()
project = store.apply("project_create", {"title": "A/B 会话隔离", "goal": "验证派发语义", "workspace": str(workspace)})
pid = project["id"]
runtime = Runtime(store)

session_a = {"id": "session-a", "workspace": str(workspace), "status": "idle"}
session_b = {"id": "session-b", "workspace": str(workspace), "status": "idle"}
runtime.poll("deepseek", {"sessions": [session_a, session_b]})
runtime.bind(pid, "deepseek", "session-a")

# --- one logical dispatch per event, and reconnect does not re-wake
store.apply("project_message", {"project_id": pid, "body": "执行这个指令"})
first = runtime.poll("deepseek", {"sessions": [session_a, session_b]})["jobs"]
check("同一事件只派发一条", len(first) == 1, first)
runtime.receipt("deepseek", {"id": first[0]["id"], "status": "delivered"})
again = runtime.poll("deepseek", {"sessions": [session_a, session_b]})["jobs"]
check("重连不重复唤醒同一事件", again == [], again)
record = next(j for j in runtime.snapshot()["jobs"] if j["id"] == first[0]["id"])
check("派发记录带会话与轮次",
      record["session"] == "session-a" and record["detail"].get("generation") == 1, record)

# --- several pending notifications coalesce into one turn, keeping event ids
store.apply("project_message", {"project_id": pid, "body": "待处理一"})
store.apply("project_message", {"project_id": pid, "body": "待处理二"})
store.apply("project_message", {"project_id": pid, "body": "待处理三"})
busy_a = [dict(session_a, status="running"), session_b]
check("忙碌会话不产生新 turn", runtime.poll("deepseek", {"sessions": busy_a})["jobs"] == [])
merged = runtime.poll("deepseek", {"sessions": [session_a, session_b]})["jobs"]
check("多条待处理合并为一次唤醒", len(merged) == 1, merged)
check("合并后保留全部事件 id", len(merged[0].get("merged_events", [])) == 2, merged[0].get("merged_events"))
check("合并内容进入同一提示", "待处理二" in merged[0]["prompt"])
runtime.receipt("deepseek", {"id": merged[0]["id"], "status": "delivered"})

# --- a switch never lets old-session work run in the new session
store.apply("task_create", {"project_id": pid, "title": "隔离板块", "owner": "deepseek", "acceptance": "只由旧会话执行"})
store.apply("dispatch", {"project_id": pid})
running_a = [dict(session_a, status="running"), session_b]
runtime.poll("deepseek", {"sessions": running_a})
refused = False
try:
    runtime.bind(pid, "deepseek", "session-b")
except Exception:
    refused = True
check("旧会话执行中拒绝换绑", refused)
idle_a = [session_a, session_b]
claimed = runtime.poll("deepseek", {"sessions": idle_a})["jobs"]
for pending in claimed:
    runtime.receipt("deepseek", {"id": pending["id"], "status": "delivered"})
runtime.bind(pid, "deepseek", "session-b")
binding = next(b for b in runtime.snapshot()["bindings"] if b["project"] == pid)
check("换绑后绑定新会话并提升轮次", binding["session"] == "session-b" and binding["generation"] == 2, binding)
late = runtime.poll("deepseek", {"sessions": [session_a, session_b]})["jobs"]
check("旧会话的历史事件不会串到新会话", all(j["session"] == "session-b" for j in late), late)

# --- a receipt from the previous binding is not trusted as delivered
store.apply("project_message", {"project_id": pid, "body": "换绑后的指令"})
job = runtime.poll("deepseek", {"sessions": [session_a, session_b]})["jobs"][0]
with runtime.store.connect() as db:
    db.execute("UPDATE native_binding_meta SET generation=generation+1 WHERE project=? AND agent=?", (pid, "deepseek"))
runtime.receipt("deepseek", {"id": job["id"], "status": "delivered", "detail": "来自上一代会话"})
stored = next(j for j in runtime.snapshot()["jobs"] if j["id"] == job["id"])
check("上一代回执记为不确定", stored["status"] == "uncertain", stored["status"])

temp.cleanup()
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"))
print(json.dumps({"passed": passed, "total": len(results)}, ensure_ascii=False))
sys.exit(0 if passed == len(results) else 1)
