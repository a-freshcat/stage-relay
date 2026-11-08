"""Compact wake briefs and compact inbox projections.

A wake must carry what the member needs to act *now*: the event that fired, the
object it references, and where to read the rest. The whole project — including
evidence of finished boards, resolved issues and archived chat — stays in the
database and is read on demand through `relay_project`, so nothing is lost.

Both projections are deliberately conservative: nothing is dropped that a member
needs in order to decide, and every shortened list is reported with its real
total so a member can tell a summary from the complete record. Wakes whose whole
purpose is evidence — `report` and `review` — deliver it unclipped.
"""

TASK_FIELDS = ("id", "title", "owner", "status", "dispatched", "archived")
ISSUE_FIELDS = ("id", "title", "status", "members", "task_ids", "archived", "created")
SHORT = 400
EXCERPT = 200
MERGE_LINES = 20
# A wake never claims to carry the whole evidence. Report/review get a longer
# preview because the reader is the one who must judge them, and the wake says
# exactly where the complete text is. Nothing is silently dropped: every cut
# string states its original length and the tool that returns it.
PREVIEW_KINDS = {"report", "review"}
PREVIEW = 1200
OPEN_TASK_LIMIT = 40
OPEN_ISSUE_LIMIT = 20


def _clip(value, limit=SHORT):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + f"…（原文 {len(value)} 字，用 relay_project 读取全文）"


def trimmed(detail, limit=SHORT):
    """Bound an event body without dropping which event it is.

    Ids, recipients, statuses and short text survive unchanged; only long prose
    is shortened, and it says so and names the tool that returns the rest.
    """
    if not isinstance(detail, dict):
        return detail
    out = {}
    for key, value in detail.items():
        if isinstance(value, str):
            out[key] = _clip(value, limit)
        elif isinstance(value, list):
            out[key] = value[:12] + ([f"…共 {len(value)} 项"] if len(value) > 12 else [])
        elif isinstance(value, dict):
            out[key] = trimmed(value, limit)
        else:
            out[key] = value
    return out


def counts(project):
    return {
        "tasks": len(project.get("tasks", [])),
        "open_tasks": len([t for t in project.get("tasks", []) if t.get("status") != "done"]),
        "issues": len(project.get("issues", [])),
        "open_issues": len([i for i in project.get("issues", []) if i.get("status") == "open"]),
        "chat": len(project.get("chat", [])),
    }


def project_summary(project):
    """The project as one screen: what is live, and how much history exists.

    Only work that can still change travels with a wake, and even that is capped
    so a project with hundreds of open boards cannot inflate every notification.
    Finished boards, resolved issues and archived chat are reported as counts —
    the member knows they exist and reads them through `relay_project` when a
    decision needs them. This is what keeps a wake the same size in year two as
    in week one.
    """
    tasks = [t for t in project.get("tasks", []) if not t.get("archived")]
    issues = [i for i in project.get("issues", []) if not i.get("archived")]
    live_tasks, done_tasks = [t for t in tasks if t.get("status") != "done"], [t for t in tasks if t.get("status") == "done"]
    open_issues, closed_issues = [i for i in issues if i.get("status") == "open"], [i for i in issues if i.get("status") != "open"]
    shown_tasks, hidden_tasks = live_tasks[:OPEN_TASK_LIMIT], max(0, len(live_tasks) - OPEN_TASK_LIMIT)
    shown_issues, hidden_issues = open_issues[:OPEN_ISSUE_LIMIT], max(0, len(open_issues) - OPEN_ISSUE_LIMIT)
    return {
        "id": project["id"],
        "title": project["title"],
        "goal": _clip(project.get("goal", "")),
        "demo": project.get("demo", False),
        "paused": project.get("paused", False),
        "closed": project.get("closed", False),
        "closed_at": project.get("closed_at"),
        "retention_days": project.get("retention_days"),
        "assignments": project.get("assignments", {}),
        "workspace": project.get("workspace", ""),
        "summary_requested": project.get("summary_requested", False),
        "review_epoch": project.get("review_epoch", 0),
        "counts": counts(project),
        "tasks": [{k: t.get(k) for k in TASK_FIELDS} for t in shown_tasks],
        "issues": [{k: i.get(k) for k in ISSUE_FIELDS} for i in shown_issues],
        "omitted": {
            "done_tasks": len(done_tasks),
            "resolved_issues": len(closed_issues),
            "open_tasks_not_listed": hidden_tasks,
            "open_issues_not_listed": hidden_issues,
            "hint": "本摘要只含仍可能变化的工作且已截断；完整列表、证据与历史用 relay_project / relay_list 读取。",
        },
    }


def chat_tail(project, limit=5):
    chat = [m for m in project.get("chat", []) if not m.get("archived")]
    return [
        {"id": m["id"], "actor": m["actor"], "created": m.get("created"), "excerpt": _clip(m.get("body", ""), EXCERPT)}
        for m in chat[-limit:]
    ], len(chat)


def find(items, item_id):
    return next((x for x in items or [] if x.get("id") == item_id), None)


def task_view(task, preview=False):
    if not task:
        return None
    view = {k: task.get(k) for k in ("id", "title", "owner", "status", "acceptance", "dispatched", "archived", "before_block", "reviewed_by")}
    # A report or a review wake exists to bring evidence to whoever must judge
    # it, so it gets a longer preview — but never a claim of completeness: the
    # preview states its original length and the tool that returns all of it.
    limit = PREVIEW if preview else SHORT
    view["evidence"] = _clip(task.get("evidence", ""), limit)
    view["review"] = _clip(task.get("review", ""), limit)
    view["review_history"] = len(task.get("review_history", []) or [])
    if preview:
        view["full_evidence_via"] = "relay_task"
    return view


def issue_view(issue, message_id=None):
    if not issue:
        return None
    messages = [m for m in issue.get("messages", []) if not m.get("archived")]
    focus = find(messages, message_id) if message_id else None
    picks = [focus] if focus else messages[-1:]
    return {
        "id": issue["id"], "title": issue["title"], "status": issue["status"],
        "members": issue["members"], "task_ids": issue.get("task_ids", []),
        "archived": issue.get("archived", False), "created": issue.get("created"),
        "choice": issue.get("choice"), "decision_note": _clip(issue.get("decision_note", "")),
        "options": [{"id": o["id"], "title": o["title"], "detail": _clip(o.get("detail", ""))} for o in issue.get("options", [])],
        "messages_total": len(messages),
        "focus_messages": [
            {"id": m["id"], "actor": m["actor"], "created": m.get("created"),
             "body": _clip(m.get("body", "")) if focus else _clip(m.get("body", ""), EXCERPT),
             "attachments": [f.get("name") for f in m.get("attachments", [])],
             "decision": m.get("decision", False)}
            for m in picks
        ],
        "read_hint": "全部历史消息用 relay_project 的 issue_id 读取。",
    }


def chat_view(project, message_id=None):
    messages = [m for m in project.get("chat", []) if not m.get("archived")]
    focus = find(messages, message_id) if message_id else None
    picks = [focus] if focus else messages[-1:]
    tail, total = chat_tail(project)
    return {
        "messages_total": total,
        "focus_messages": [
            {"id": m["id"], "actor": m["actor"], "created": m.get("created"),
             "body": _clip(m.get("body", "")),
             "attachments": [f.get("name") for f in m.get("attachments", [])]}
            for m in picks
        ],
        "recent": tail if not focus else [],
        "read_hint": "完整群聊用 relay_project 读取。",
    }


def brief(project, agent, kind, actor, detail, seq=None):
    """One wake, bounded by the event rather than by the project's age."""
    preview = kind in PREVIEW_KINDS
    data = {
        "project": project_summary(project),
        "member": agent,
        "event": {
            "seq": seq, "kind": kind, "actor": actor,
            "detail": trimmed(detail, PREVIEW if preview else SHORT),
        },
    }
    context = {}
    task = task_view(find(project.get("tasks", []), detail.get("task_id")), preview=preview)
    if task:
        context["task"] = task
    issue = issue_view(find(project.get("issues", []), detail.get("issue_id")), detail.get("message_id"))
    if issue:
        context["issue"] = issue
    if kind in {"project_message", "record_archive", "record_restore"}:
        context["chat"] = chat_view(project, detail.get("message_id"))
    if kind in {"summary_request", "summary_submit"} and project.get("summary"):
        context["summary"] = _clip(project["summary"].get("body", ""))
    if context:
        data["context"] = context
    data["read_hint"] = "遇到事件之外的问题用 relay_project / relay_task / relay_issue / relay_message / relay_event / relay_list 按需读取全文。"
    return data
