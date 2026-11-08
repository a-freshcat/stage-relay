"""Build a UI fixture for qa/ui_probe.mjs from isolated projects.

Everything runs under qa/; the real data directory is never opened.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.domain import Store

root = Path(__file__).resolve().parent / "ui-fixture"
root.mkdir(exist_ok=True)
store = Store(root / "workspace.sqlite3")
state = store.snapshot()


def find(title):
    return next((p for p in state["projects"] if p["title"] == title), None)


def new_project(title, goal):
    workspace = root / f"ws-{title}"
    workspace.mkdir(exist_ok=True)
    return store.apply(
        "project_create",
        {
            "title": title, "goal": goal, "workspace": str(workspace.resolve()),
            "assignments": {"planner": "codex", "executor": "deepseek", "reporter": "deepseek"},
        },
    )


closed = find("结束态渲染")
if not closed:
    project = new_project("结束态渲染", "验证结束态面板、资源登记与清理预览的渲染。")
    pid = project["id"]
    task = store.apply("task_create", {"project_id": pid, "title": "已完成板块", "owner": "deepseek", "acceptance": "有检查依据"})
    store.apply("dispatch", {"project_id": pid})
    store.apply("report", {"project_id": pid, "task_id": task["id"], "status": "review", "evidence": "执行并读回文件。"}, "deepseek")
    store.apply("review", {"project_id": pid, "task_id": task["id"], "approved": True, "evidence": "独立核验通过。"}, "codex")
    store.apply("task_create", {"project_id": pid, "title": "未完成板块", "owner": "deepseek", "acceptance": "结束时撤销"})
    store.apply("project_message", {"project_id": pid, "body": "结束前最后一次交接。"})
    store.apply("project_close", {"project_id": pid, "retention_days": 3})
    state = store.snapshot()

open_project = find("进行中任务")
if not open_project:
    open_project = new_project("进行中任务", "验证结束确认弹窗。")
    store.apply("task_create", {"project_id": open_project["id"], "title": "进行中板块", "owner": "deepseek", "acceptance": "尚未派发"})
    state = store.snapshot()

closed = find("结束态渲染")
open_project = find("进行中任务")
state["projects"] = [closed, open_project]
state["runtime"] = {
    "hosts": [], "bindings": [], "jobs": [], "activity": [],
    "resources": [{"id": "resource_fixture", "project": closed["id"], "agent": "deepseek", "session": "dsh-native-demo",
                   "origin": "panel-created", "parent": "", "created": 0, "status": "archived",
                   "detail": "原应用已完成归档"}],
}
state["revision"] = 1
(root / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
print(closed["id"], open_project["id"], [t["status"] for t in closed["tasks"]])
