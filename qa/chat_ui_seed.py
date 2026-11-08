import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.domain import Store

root = Path(__file__).resolve().parent / "chat-ui"
store = Store(root / "workspace.sqlite3")
if not any(p["title"] == "阶段验收：项目群聊" for p in store.snapshot()["projects"]):
    demo = store.apply("demo_create", {})
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    project = store.apply(
        "project_create",
        {
            "title": "阶段验收：项目群聊",
            "goal": "验证真实项目在没有议题时也能打开群聊，并且刷新后不丢。",
            "workspace": str(workspace.resolve()),
            "assignments": {"planner": "codex", "executor": "deepseek", "reporter": "deepseek"},
        },
    )
    pid = project["id"]
    for actor, body in [
        ("user", "真实项目没有议题时，群聊入口在哪里？"),
        ("deepseek", "顶栏、任务总览和议题页空态都有入口；刷新后保持上次打开的线程。"),
        ("codex", "已读回 web/app.js，确认项目群聊与议题群聊并存。"),
    ]:
        store.apply("project_message", {"project_id": pid, "body": body}, actor)
    print(json.dumps({"project": pid, "demo": demo["id"]}, ensure_ascii=False))
else:
    print("exists")
