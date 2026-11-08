"""Measure idle-poll and wake metrics from a specified source tree.

The baseline must come from the code that actually shipped, not from a guess:
with `--source head` the script checks the committed package out of git into a
throwaway directory and measures *that*; with `--source worktree` it copies the
current files. Both runs use the identical measurement, so the comparison is
like for like.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("domain.py", "runtime.py", "brief.py", "rules.py")

CHILD = r'''
import json, statistics, sys, time, tempfile, shutil
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from relay.domain import Store
from relay.runtime import Runtime

EVIDENCE = "检查命令 python -m unittest discover -s tests -q 通过；" * 40
MESSAGE = "这一轮把执行会话与派发记录对齐，附上实测输出与文件哈希。" * 8

def build(store, workspace, tasks, messages, issues=0, issue_messages=0):
    project = store.apply("project_create", {"title": "上下文基线项目", "goal": "验证一次唤醒携带的数据量", "workspace": str(workspace)})
    pid = project["id"]
    for n in range(tasks):
        task = store.apply("task_create", {"project_id": pid, "title": f"板块 {n}", "acceptance": "检查命令成功", "owner": "deepseek"})
        store.apply("dispatch", {"project_id": pid})
        store.apply("report", {"project_id": pid, "task_id": task["id"], "status": "review", "evidence": f"第 {n} 轮依据：{EVIDENCE}"}, "deepseek")
        store.apply("review", {"project_id": pid, "task_id": task["id"], "approved": True, "evidence": f"独立核验第 {n} 轮：{EVIDENCE}"}, "codex")
    for n in range(messages):
        store.apply("project_message", {"project_id": pid, "body": f"{n} {MESSAGE}"})
    for n in range(issues):
        issue = store.apply("issue_create", {"project_id": pid, "title": f"议题 {n}", "body": MESSAGE, "members": ["codex", "deepseek"], "options": [{"title": "A", "detail": MESSAGE}, {"title": "B", "detail": MESSAGE}]}, "deepseek")
        for m in range(issue_messages):
            store.apply("message", {"project_id": pid, "issue_id": issue["id"], "body": f"{m} {MESSAGE}"}, "deepseek")
    return pid

root = Path(sys.argv[2])
report = {}
for label, tasks, messages in (("small", 1, 1), ("long", 25, 40)):
    temp = Path(tempfile.mkdtemp(prefix="pkg-", dir=root))
    store = Store(temp / "state.sqlite3")
    runtime = Runtime(store)
    pid = build(store, temp, tasks, messages, issues=8, issue_messages=15)
    session = "poll-" + label
    sessions = [{"id": session, "workspace": str(temp), "status": "idle"}]
    runtime.poll("deepseek", {"app": "dsh", "sessions": sessions})
    runtime.bind(pid, "deepseek", session)
    store.apply("project_message", {"project_id": pid, "body": "预热"}, "user")
    while True:
        pending = runtime.poll("deepseek", {"app": "dsh", "sessions": sessions})["jobs"]
        if not pending:
            break
        for job in pending:
            runtime.receipt("deepseek", {"id": job["id"], "status": "delivered"})
    samples = []
    for _ in range(40):
        start = time.perf_counter()
        result = runtime.poll("deepseek", {"app": "dsh", "sessions": sessions})
        samples.append(time.perf_counter() - start)
        assert result["jobs"] == []
    store.apply("project_message", {"project_id": pid, "body": "一次稳态唤醒"}, "user")
    jobs = [j for j in runtime.poll("deepseek", {"app": "dsh", "sessions": sessions})["jobs"] if j["session"] == session]
    inbox = store.inbox("deepseek", after=0)
    mine = [p for p in inbox["projects"] if p["id"] == pid]
    report[label] = {
        "idle_poll_ms_median": round(statistics.median(samples) * 1000, 2),
        "idle_poll_ms_p95": round(sorted(samples)[int(len(samples) * 0.95)] * 1000, 2),
        "wake_prompt_bytes": max((len(j.get("prompt", "").encode()) for j in jobs), default=0),
        "wake_job_bytes": max((len(json.dumps(j, ensure_ascii=False).encode()) for j in jobs), default=0),
        "inbox_bytes": len(json.dumps(inbox, ensure_ascii=False).encode()),
        "inbox_project_bytes": len(json.dumps(mine, ensure_ascii=False).encode()),
        "events_in_project": len([e for e in store.snapshot()["events"] if e["project"] == pid]),
    }
    shutil.rmtree(temp, ignore_errors=True)
print(json.dumps(report, ensure_ascii=False))
'''


def run(source, label):
    temp = Path(tempfile.mkdtemp(prefix="context-tree-", dir=ROOT / "qa"))
    try:
        (temp / "relay").mkdir()
        (temp / "relay" / "__init__.py").write_text('__version__ = "measured"\n', encoding="utf-8")
        for module in MODULES:
            target = temp / "relay" / module
            if source == "head":
                got = subprocess.run(["git", "show", f"HEAD:relay/{module}"], cwd=ROOT,
                                     capture_output=True, check=False)
                if got.returncode:
                    continue  # a module that did not exist in that commit
                target.write_bytes(got.stdout)
            else:
                shutil.copy(ROOT / "relay" / module, target)
        result = subprocess.run([sys.executable, "-c", CHILD, str(temp), str(ROOT / "qa")],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            print(result.stdout[-3000:], result.stderr[-3000:])
            raise SystemExit(1)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        out = ROOT / "qa" / f"context-poll-{label}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{label}]")
        for project in ("small", "long"):
            print(f"  {project:5} idle_ms={report[project]['idle_poll_ms_median']:>8} "
                  f"wake_bytes={report[project]['wake_prompt_bytes']:>7} "
                  f"inbox_project_bytes={report[project]['inbox_project_bytes']:>7}")
        print(f"  written: {out.relative_to(ROOT)}")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else "head"
    run(source, "before" if source == "head" else "after")
