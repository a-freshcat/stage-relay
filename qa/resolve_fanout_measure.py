"""Measure what one issue-resolution event costs today, before changing anything.

Codex asked for a scoped evaluation, not a patch mixed into a published release.
This runs an isolated store, resolves one issue, and reports how many native jobs
each member receives and how large each wake prompt is. It changes nothing.

Usage: python qa/resolve_fanout_measure.py [messages]
"""

import json
import sys
import tempfile
from pathlib import Path

from relay.domain import Store
from relay.runtime import Runtime

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = int(sys.argv[1]) if len(sys.argv) > 1 else 12


def build(temp):
    store = Store(Path(temp) / "state.sqlite3")
    runtime = Runtime(store)
    project = store.apply("project_create", {
        "title": "唤醒扇出测量", "goal": "测量一次议题解决的实际唤醒", "workspace": temp})
    project_id = project["id"]
    store.apply("assign", {"project_id": project_id,
                           "assignments": {"planner": "codex", "executor": "deepseek", "reporter": "codex"}})
    sessions = [{"id": "s-codex", "workspace": temp, "status": "idle"},
                {"id": "s-deepseek", "workspace": temp, "status": "idle"}]
    runtime.poll("codex", {"app": "codex", "sessions": sessions})
    runtime.poll("deepseek", {"app": "dsh", "sessions": sessions})
    runtime.bind(project_id, "codex", "s-codex")
    runtime.bind(project_id, "deepseek", "s-deepseek")
    return store, runtime, project_id, sessions


def drain(runtime, sessions):
    """Read whatever each member was queued, without pretending to run it."""
    out = {}
    for agent, app in (("codex", "codex"), ("deepseek", "dsh")):
        jobs = runtime.poll(agent, {"app": app, "sessions": sessions})["jobs"]
        out[agent] = [{"bytes": len(j["prompt"].encode("utf-8")),
                       "kind": j.get("kind"), "event": j.get("event")} for j in jobs]
    return out


def main():
    temp = tempfile.mkdtemp(prefix="resolve-measure-", dir=ROOT / "qa")
    store, runtime, project_id, sessions = build(temp)
    drain(runtime, sessions)  # clear the project-creation wakes

    issue = store.apply("issue_create", {
        "project_id": project_id, "title": "测量议题",
        "body": "测量一次议题解决会唤醒多少成员。",
        "members": ["codex", "deepseek"], "task_ids": []})
    for n in range(MESSAGES):
        store.apply("message", {
            "project_id": project_id, "issue_id": issue["id"],
            "body": f"第 {n} 条讨论内容，" + "细节说明。" * 40}, "codex" if n % 2 else "deepseek")
    drain(runtime, sessions)  # clear discussion wakes so only resolve is measured

    store.apply("resolve", {"project_id": project_id, "issue_id": issue["id"]})
    measured = drain(runtime, sessions)

    print(f"issue messages: {MESSAGES}")
    for agent, jobs in measured.items():
        print(f"{agent}: {len(jobs)} wake(s)")
        for job in jobs:
            print(f"   kind={job['kind']} event={job['event']} prompt={job['bytes']} bytes")

    print("\n-- what each resolve wake carries --")
    first = None
    for agent, jobs in measured.items():
        if jobs and first is None:
            first = agent
    for agent, app in (("codex", "codex"), ("deepseek", "dsh")):
        store.apply("project_message", {"project_id": project_id, "body": f"锚点 {agent}"})
    drain(runtime, sessions)

    store.apply("issue_create", {"project_id": project_id, "title": "第二个议题",
                                 "body": "第二次解决的唤醒体积。",
                                 "members": ["codex", "deepseek"], "task_ids": []})
    second = [i for i in store.snapshot()["projects"][0]["issues"] if i["title"] == "第二个议题"][0]
    drain(runtime, sessions)
    store.apply("resolve", {"project_id": project_id, "issue_id": second["id"]})
    again = drain(runtime, sessions)
    for agent, jobs in again.items():
        for job in jobs:
            print(f"{agent}: kind={job['kind']} prompt={job['bytes']} bytes")

    total = sum(len(j) for j in measured.values())
    print(f"\nTOTAL wakes for one plain resolution: {total}")
    print(f"isolated store: {temp}")


if __name__ == "__main__":
    main()