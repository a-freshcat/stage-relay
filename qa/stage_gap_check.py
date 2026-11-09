"""Check the exact gap Codex reported: supervisor == reporter must be able to submit.

The prior message told that role to "submit directly" and then the submit path
refused with "user has not requested a summary". This asserts the real behaviour
of the current source, without guessing.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.domain import Invalid, Store  # noqa: E402

temp = tempfile.TemporaryDirectory(prefix="test-state-", dir=ROOT / "qa")
store = Store(Path(temp.name) / "workspace.sqlite3")
try:
    project = store.apply("project_create", {"title": "阶段验证", "goal": "验证",
                                             "workspace": temp.name})
    pid = project["id"]
    # codex is both supervisor and reporter, the case that was refused.
    store.apply("assign", {"project_id": pid,
                           "assignments": {"planner": "codex", "executor": "deepseek",
                                           "reporter": "codex"}})
    task = store.apply("task_create", {"project_id": pid, "title": "板块",
                                       "acceptance": "读回", "owner": "deepseek"})
    store.apply("dispatch", {"project_id": pid})
    store.apply("report", {"project_id": pid, "task_id": task["id"], "status": "review",
                           "evidence": "检查通过"}, "deepseek")
    store.apply("review", {"project_id": pid, "task_id": task["id"], "approved": True,
                           "evidence": "核验通过", "review_epoch": 0}, "codex")

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))

    # 1. Asking yourself: refused, with the "submit directly" hint.
    try:
        store.apply("summary_request", {"project_id": pid}, "codex")
        check("监督兼汇总请求自己被拒", False, "no error raised")
    except Invalid as error:
        check("监督兼汇总请求自己被拒", "直接提交" in str(error), error)

    # 2. Direct submission by that same role must now work.
    try:
        submitted = store.apply("summary_submit", {"project_id": pid,
                                                   "body": "阶段完成说明"}, "codex")
        check("监督兼汇总可直接提交", bool(submitted), submitted)
    except Invalid as error:
        check("监督兼汇总可直接提交", False, error)

    # 3. A different stage keeps its own history instead of overwriting.
    issue = store.apply("issue_create", {"project_id": pid, "title": "阶段二",
                                        "body": "问题", "members": ["codex", "deepseek"],
                                        "options": [{"title": "A", "detail": "a"},
                                                    {"title": "B", "detail": "b"}]}, "deepseek")
    store.apply("resolve", {"project_id": pid, "issue_id": issue["id"],
                            "choice": issue["options"][0]["id"]}, "user")
    store.apply("assign", {"project_id": pid,
                           "assignments": {"planner": "codex", "executor": "deepseek",
                                          "reporter": "antigravity"}})
    stage = store.apply("summary_request", {"project_id": pid,
                                            "issue_id": issue["id"]}, "codex")
    store.apply("summary_submit", {"project_id": pid, "stage_id": stage["id"],
                                   "body": "阶段二说明"}, "antigravity")
    snapshot = store.snapshot()
    stages = snapshot["projects"][0].get("stages", [])
    check("不同阶段历史并存", len(stages) == 1 and stages[0]["status"] == "submitted", stages)
    check("阶段条目有修订号", stages[0].get("revision") == 1, stages[0])
    print(json.dumps({"results": results}, ensure_ascii=False))
finally:
    temp.cleanup()
