"""Reproduce the reviewer's three blockers on the current source.

1) execute must persist `executed` on the plan (not the lock) so a stale lock can
   never re-run a finished plan or overwrite its recovery manifest.
2) bind must refuse while the old session is still running.
3) scratch registration must not adopt an unmarked pre-existing tmp directory.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.cleanup import Cleanup, OWNER_MARKER, scratch_for
from relay.domain import Invalid, Store
from relay.runtime import Runtime

root = Path(__file__).resolve().parent / "rework-verify"
root.mkdir(exist_ok=True)
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))


temp = tempfile.TemporaryDirectory(prefix="probe-", dir=root)
data = Path(temp.name)
store = Store(data / "workspace.sqlite3")
workspace = data / "ws"
workspace.mkdir()
project = store.apply("project_create", {"title": "返修复核", "goal": "复核三个阻断项", "workspace": str(workspace)})
pid = project["id"]
uploads = data / "uploads"
uploads.mkdir()

# --- 3) an unmarked pre-existing tmp directory must not be adopted
stray = data / "tmp" / pid
stray.mkdir(parents=True)
(stray / "foreign.bin").write_bytes(b"f" * 8)
old = time.time() - 40 * 86400
os.utime(stray / "foreign.bin", (old, old))
scratch_for(data, pid)
check("未登记 tmp 目录不被认领", not (stray / OWNER_MARKER).exists(),
      [p.name for p in stray.iterdir()])

plan_project = next(p for p in store.snapshot()["projects"] if p["id"] == pid)
preview = Cleanup(data).preview(plan_project, store.snapshot()["projects"], retention_days=1)
check("未认领目录不会成为清理候选",
      [i for i in preview["items"] if "foreign.bin" in i["path"]] == [])

# --- 1) a finished plan stays finished, and a stale lock cannot re-run it
file_id = "b" * 24
(uploads / file_id).write_bytes(b"x" * 16)
(uploads / f"{file_id}.json").write_text(json.dumps({"id": file_id, "name": "孤儿.txt", "size": 16, "project": pid}), encoding="utf-8")
for path in (uploads / file_id, uploads / f"{file_id}.json"):
    os.utime(path, (old, old))
store.apply("project_close", {"project_id": pid, "retention_days": 1})
cleanup = Cleanup(data)
closed = next(p for p in store.snapshot()["projects"] if p["id"] == pid)
plan = cleanup.preview(closed, store.snapshot()["projects"])
first = cleanup.execute(closed, store.snapshot()["projects"], plan["id"])
check("首次执行移动了候选", len(first["moved"]) == 2, first["moved"])
plan_state = json.loads((data / "cleanup-plans" / f"{plan['id']}.json").read_text(encoding="utf-8"))
check("计划文件写入 executed", plan_state.get("executed") is True, plan_state.get("executed"))
lock = data / "cleanup-plans" / f"{plan['id']}.lock"
check("执行后释放锁", not lock.exists())
# Inject a stale lock exactly as the reviewer described.
lock.write_text("stale", encoding="utf-8")
os.utime(lock, (time.time() - 5000, time.time() - 5000))
repeat_refused = False
try:
    cleanup.execute(closed, store.snapshot()["projects"], plan["id"])
except Invalid:
    repeat_refused = True
check("过期锁不能让已执行计划重跑", repeat_refused)
manifest = json.loads((Path(first["root"]) / "manifest.json").read_text(encoding="utf-8"))
check("恢复依据没有被覆盖", len(manifest["moved"]) == 2, manifest["moved"])

# --- 2) rebinding is refused while the old session still runs
runtime = Runtime(store)
sessions = [{"id": "s-one", "workspace": str(workspace), "status": "idle"},
            {"id": "s-two", "workspace": str(workspace), "status": "idle"}]
probe = store.apply("project_create", {"title": "换绑复核", "goal": "旧会话运行中拒绝换绑", "workspace": str(workspace)})
runtime.poll("deepseek", {"sessions": sessions})
runtime.bind(probe["id"], "deepseek", "s-one")
busy = [{"id": "s-one", "workspace": str(workspace), "status": "running"},
        {"id": "s-two", "workspace": str(workspace), "status": "idle"}]
runtime.poll("deepseek", {"sessions": busy})
refused = False
try:
    runtime.bind(probe["id"], "deepseek", "s-two")
except Invalid:
    refused = True
check("旧会话运行中拒绝换绑", refused)
idle = [{"id": "s-one", "workspace": str(workspace), "status": "idle"},
        {"id": "s-two", "workspace": str(workspace), "status": "idle"}]
runtime.poll("deepseek", {"sessions": idle})
runtime.bind(probe["id"], "deepseek", "s-two")
binding = next(b for b in runtime.snapshot()["bindings"] if b["project"] == probe["id"])
check("旧会话空闲后换绑成功并提升轮次", binding["session"] == "s-two" and binding["generation"] == 2, binding)

temp.cleanup()
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" :: {detail}"))
print(json.dumps({"passed": passed, "total": len(results)}, ensure_ascii=False))
sys.exit(0 if passed == len(results) else 1)
