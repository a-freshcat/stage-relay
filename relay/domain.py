"""Transactional workspace, decisions, and cooperative agent mailboxes.

No model calls and no desktop process control live here. An assignment is queued
until its assigned application explicitly acknowledges it through the bridge.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import brief, rules


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


class Invalid(ValueError):
    pass


def text(value, label="内容", limit=10000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Invalid(f"{label}不能为空，且不能超过 {limit} 字。")
    return value.strip()


def get(items, item_id, label):
    found = next((item for item in items if item["id"] == item_id), None)
    if found is None:
        raise Invalid(f"找不到{label}，请刷新后重试。")
    return found


def initial_state():
    return {
        "version": 1,
        "agents": [
            {"id": "codex", "name": "Codex", "app": "codex", "last_seen": None, "session": ""},
            {"id": "deepseek", "name": "DeepSeek · DSH", "app": "dsh", "last_seen": None, "session": ""},
            {"id": "antigravity", "name": "Antigravity", "app": "antigravity", "last_seen": None, "session": ""},
        ],
        "projects": [],
    }


def make_project(title, goal, assignments, demo=False, workspace=""):
    return {
        "id": uid("project"), "title": title, "goal": goal, "demo": demo,
        "created": now(), "paused": False, "assignments": assignments, "workspace": workspace,
        "tasks": [], "issues": [], "chat": [], "summary": None, "summary_requested": False,
        "closed": False, "closed_at": None, "retention_days": None,
    }


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.archive_dir = self.path.parent / "deleted-projects"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, kind TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, created TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO workspace VALUES (1, ?)", (json.dumps(initial_state(), ensure_ascii=False),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _state(raw):
        """Load persisted state and add fields introduced by later versions.

        Older projects predate the project-level chat thread; reading must not
        rewrite their issues, decisions, or evidence.
        """
        state = json.loads(raw) if isinstance(raw, str) else raw
        for project in state.get("projects", []):
            project.setdefault("chat", [])
            project.setdefault('review_epoch',0)
            project.setdefault("closed", False)
            project.setdefault("closed_at", None)
            project.setdefault("retention_days", None)
        return state

    def snapshot(self):
        with self.connect() as db:
            db.execute("BEGIN")
            state = self._state(db.execute("SELECT body FROM workspace WHERE id=1").fetchone()[0])
            rows = db.execute("SELECT seq, project, kind, actor, body, created FROM events ORDER BY seq DESC LIMIT 120").fetchall()
        state["events"] = [dict(seq=r[0], project=r[1], kind=r[2], actor=r[3], detail=json.loads(r[4]), created=r[5]) for r in rows]
        state["revision"] = rows[0][0] if rows else 0
        return state

    @staticmethod
    def _event(db, project, kind, actor, detail):
        db.execute("INSERT INTO events(project,kind,actor,body,created) VALUES(?,?,?,?,?)", (project, kind, actor, json.dumps(detail, ensure_ascii=False), now()))

    def inbox(self, agent_id, after=None, full=False, ack=None):
        """Incremental read: no `after` means "continue from my acknowledged point".

        `full=true` re-reads the event log from the very beginning, page by page.
        It does not expand evidence: the projections stay summaries either way,
        and complete text comes from relay_project / relay_task / relay_issue /
        relay_message / relay_event / relay_list. The wording here says exactly
        that, because "full" that still returns clipped evidence would be a lie.

        The position never advances on its own. A member sees the same events
        until it says, in a later call, that it actually has them:
        `ack=<next_ack>`. That is the only proof the previous reply reached the
        caller — "the caller called again" is not, because a retry, a second
        bridge and a dropped response are indistinguishable. Until the ack
        arrives the events stay in the window, so a lost reply costs a
        duplicate, never a loss. `uncertain` deliveries are still never re-sent:
        reading is not executing.
        """
        if full:
            after = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db.execute("SELECT body FROM workspace WHERE id=1").fetchone()[0])
            agent = get(state["agents"], agent_id, "成员")
            agent["last_seen"] = now()
            revision = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            confirmed = int(agent.get("inbox_acked") or 0)
            if ack is not None:
                if not isinstance(ack, int) or isinstance(ack, bool) or ack < 0:
                    raise Invalid("确认位置无效。")
                if ack > confirmed:
                    agent["inbox_acked"] = ack
                    confirmed = ack
                    db.execute("UPDATE workspace SET body=? WHERE id=1",
                               (json.dumps(state, ensure_ascii=False),))
            if after is not None:
                start = max(0, int(after))
            else:
                start = confirmed
            rows = db.execute("SELECT seq, project, kind, actor, body, created FROM events WHERE seq>? ORDER BY seq LIMIT 250", (start,)).fetchall()
        projects = [p for p in state["projects"] if not p["demo"] and (agent_id in p["assignments"].values() or any(t["owner"] == agent_id for t in p["tasks"]) or any(agent_id in i["members"] for i in p["issues"]))]
        project_ids = {p["id"] for p in projects}
        events = []
        for seq, project, kind, actor, body, created in rows:
            detail = json.loads(body)
            if project in project_ids and agent_id in detail.get("recipients", []):
                events.append(dict(seq=seq, project=project, kind=kind, actor=actor, detail=brief.trimmed(detail), created=created))
        cursor = rows[-1][0] if rows else revision
        return {
            "agent_id": agent_id,
            # Summaries, not the whole record: the goal, the structure and the
            # state now, with evidence and full history one explicit read away.
            "projects": [brief.project_summary(p) for p in projects],
            "events": events,
            "cursor": cursor,
            "from_seq": start,
            "acked": confirmed,
            "next_ack": cursor,
            "ack_hint": "确认收到后再调用时传 ack=<next_ack>；不传则下次仍从同一位置开始。",
            "has_more": len(rows) == 250,
            "page": {"size": 250, "returned": len(rows), "next_after": cursor,
                     "note": "还有更多事件时用 after=<next_after> 继续读取，不丢事件。"},
            "rules": {"version": rules.VERSION, "digest": rules.digest(agent_id), "tool": "relay_rules"},
            "read": {
                "project": "relay_project project_id=… 读取完整项目（含全部证据、群聊与议题历史）",
                "task": "relay_task project_id task_id",
                "issue": "relay_issue project_id issue_id",
                "message": "relay_message project_id message_id [issue_id] 读取单条消息原文",
                "list": "relay_list project_id kind=tasks|issues|chat|messages|events 分页读取完整条目，无需读整个项目",
                "event": "relay_event project_id seq",
                "rules": "relay_rules 规则原文",
                "history": "从头分页重读事件日志传 full=true 或 after=0；这两者只扩大事件范围，证据仍是摘要。",
                "complete": "完整证据与全文用 relay_project / relay_task / relay_issue / relay_message / relay_list 读取。"
            },
            "notice": "paused=true 时停止领取新工作；读取当前状态后再行动。事件不表示应用已执行。",
        }

    def readable(self, agent_id, project_id):
        """A member may read the full record of projects it belongs to."""
        with self.connect() as db:
            state = self._state(db.execute("SELECT body FROM workspace WHERE id=1").fetchone()[0])
        get(state["agents"], agent_id, "成员")
        project = get(state["projects"], project_id, "任务")
        mine = (agent_id in project["assignments"].values()
                or any(t["owner"] == agent_id for t in project["tasks"])
                or any(agent_id in i["members"] for i in project["issues"]))
        if project["demo"] or not mine:
            raise Invalid("你不属于此任务，无法读取。")
        return project

    def read_project(self, agent_id, project_id):
        return self.readable(agent_id, project_id)

    def read_task(self, agent_id, project_id, task_id):
        return get(self.readable(agent_id, project_id)["tasks"], task_id, "工作板块")

    def read_issue(self, agent_id, project_id, issue_id):
        return get(self.readable(agent_id, project_id)["issues"], issue_id, "议题")

    def read_message(self, agent_id, project_id, message_id, issue_id=None):
        """One message, whole: the project thread or a named issue thread."""
        project = self.readable(agent_id, project_id)
        threads = []
        if issue_id:
            threads.append(get(project["issues"], issue_id, "议题")["messages"])
        else:
            threads.append(project.get("chat", []))
            threads.extend(issue["messages"] for issue in project["issues"])
        for thread in threads:
            found = next((m for m in thread if m["id"] == message_id), None)
            if found:
                return found
        raise Invalid("找不到这条消息，或它不属于该项目。")

    def read_list(self, agent_id, project_id, kind, offset=0, limit=50, issue_id=None):
        """Paged listing, so one entry never costs a whole project read.

        `kind` is one of tasks / issues / chat / messages / events. Everything is
        full text here; pagination is the only thing that bounds it, and the
        reply always states the total so a reader can tell a page from the whole.
        """
        project = self.readable(agent_id, project_id)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise Invalid("分页起点无效。")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise Invalid("每页条数请填 1-200。")
        if kind == "tasks":
            items = project["tasks"]
        elif kind == "issues":
            items = project["issues"]
        elif kind == "chat":
            items = project.get("chat", [])
        elif kind == "messages":
            issue = get(project["issues"], issue_id, "议题")
            items = issue["messages"]
        elif kind == "events":
            with self.connect() as db:
                rows = db.execute("SELECT seq,kind,actor,body,created FROM events WHERE project=? ORDER BY seq",
                                  (project_id,)).fetchall()
            items = [dict(seq=r[0], kind=r[1], actor=r[2], detail=json.loads(r[3]), created=r[4]) for r in rows]
        else:
            raise Invalid("分页类型仅支持 tasks / issues / chat / messages / events。")
        page = items[offset:offset + limit]
        return {
            "project_id": project_id, "kind": kind,
            "total": len(items), "offset": offset, "returned": len(page),
            "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
            "items": page,
            "note": "这是分页结果，不是全部；按 next_offset 继续读取，或直接用 relay_task / relay_issue / relay_message 取指定对象。",
        }

    def read_event(self, agent_id, project_id, seq):
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise Invalid("事件序号无效。")
        self.readable(agent_id, project_id)
        with self.connect() as db:
            row = db.execute("SELECT seq, project, kind, actor, body, created FROM events WHERE seq=?", (seq,)).fetchone()
        if not row or row[1] != project_id:
            raise Invalid("找不到这条事件，或它不属于该项目。")
        return dict(seq=row[0], project=row[1], kind=row[2], actor=row[3], detail=json.loads(row[4]), created=row[5])

    def apply(self, action, args, actor="user"):
        if not isinstance(args, dict):
            raise Invalid("请求参数必须是对象。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db.execute("SELECT body FROM workspace WHERE id=1").fetchone()[0])
            if actor != "user":
                member = get(state["agents"], actor, "成员")
                member["last_seen"] = now()
            result = self._apply(db, state, action, args, actor)
            db.execute("UPDATE workspace SET body=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))
        return result

    def _apply(self, db, state, action, a, actor):
        user_actions = {"project_create", "workspace_set", "demo_create", "demo_reset", "setup_record", "agent_save", "assign", "task_create", "task_edit", "dispatch", "pause", "resolve", "accept", "summary_request", "invite", "project_close", "record_archive", "record_restore", "project_forget", "project_restore"}
        if action in user_actions and actor != "user":
            raise Invalid("这项操作由用户决定。")
        if action == "setup_record":
            member = get(state["agents"], a.get("agent_id"), "成员")
            member["setup"] = {"configured": a["configured"], "path": a["path"], "created": now()}
            member["last_seen"] = None
            member["session"] = ""
            self._event(db, None, action, actor, {"name": member["name"], "text": "已配置接入，等待应用确认" if a["configured"] else "用户撤销了应用接入"})
            return member["setup"]
        if action == "agent_save":
            agent_id = a.get("id")
            app = a.get("app")
            if app not in {"codex", "dsh", "antigravity"}:
                raise Invalid("请选择支持的应用。")
            name = text(a.get("name"), "成员名称", 60)
            if agent_id:
                member = get(state["agents"], agent_id, "成员")
                if member["app"] != app:
                    raise Invalid("已有成员不能改绑应用，请新建成员。")
                member["name"] = name
            else:
                member = {"id": uid(app), "name": name, "app": app, "last_seen": None, "session": ""}
                state["agents"].append(member)
            self._event(db, None, action, actor, {"name": name})
            return member
        if action == "connect":
            if actor == "user":
                raise Invalid("接入需要成员身份。")
            member = get(state["agents"], actor, "成员")
            member["session"] = text(a.get("session", "MCP session"), "会话", 200)
            self._event(db, None, action, actor, {"name": member["name"]})
            return {"agent": member, "notice": "这是协作信箱。请主动读取 relay_inbox；本接口不会自行唤醒已结束的应用会话。"}
        if action in {"project_create", "demo_create"}:
            assignments = a.get("assignments", {"planner": "codex", "executor": "deepseek", "reporter": "antigravity" if action=='demo_create' else "codex"})
            self._assignments(state, assignments)
            title = text(a.get("title", "任务管理面板"), "任务名称", 100)
            goal = text(a.get("goal", "完成一个任务管理面板，明确记录方案、执行结果与交付依据。"), "目标", 6000)
            workspace = self._workspace(a.get("workspace"), a.get("create_workspace") is True) if action == "project_create" else ""
            p = make_project(title, goal, assignments, action == "demo_create", workspace)
            state["projects"].append(p)
            if p["demo"]:
                self._demo(p)
            self._event(db, p["id"], action, actor, {"text": f"创建{'演示' if p['demo'] else ''}任务：{p['title']}"})
            return p
        if action == "project_restore":
            # Recycling is reversible: the whole project is read back from its
            # recovery file, so history, chat and evidence all return intact.
            project_id = a.get("project_id")
            source = self.archive_dir / f"project-{project_id}.json" if isinstance(project_id, str) else None
            if not source or not source.is_file():
                raise Invalid("找不到这条项目记录的可恢复文件。")
            if any(x["id"] == project_id for x in state["projects"]):
                raise Invalid("这条项目记录已经在列表中。")
            project = self._state({"projects": [json.loads(source.read_text(encoding="utf-8"))]})["projects"][0]
            state["projects"].append(project)
            self._event(db, project_id, action, actor, {"text": f"用户把项目记录移回列表：{project['title']}", "project": project_id, "recipients": sorted(set(project["assignments"].values()))})
            return {"restored": project_id, "title": project["title"]}
        p = get(state["projects"], a.get("project_id"), "任务")
        members = set(p["assignments"].values()) | {t["owner"] for t in p["tasks"]} | {m for i in p["issues"] for m in i["members"]}
        if action=='review':members.update(p.get('reviewer_history',[]))
        if actor != "user" and (p["demo"] or actor not in members):
            raise Invalid("你不属于此任务，或此任务仅为演示。")
        if action == "project_forget":
            # Deleting a project removes the record from the active list only.
            # The whole project (chat, issues, decisions, evidence) is written to
            # a recovery file first, and the workspace itself is never touched.
            if p.get("demo"):
                raise Invalid("演示任务不能删除；重置即可。")
            if len(state["projects"]) <= 1:
                raise Invalid("这是最后一条项目记录，不能全部删除。")
            if not p.get("closed"):
                raise Invalid("请先结束任务，再删除项目记录。")
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            target = self.archive_dir / f"project-{p['id']}.json"
            target.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
            state["projects"] = [x for x in state["projects"] if x["id"] != p["id"]]
            self._event(db, None, action, actor, {"text": f"用户删除了项目记录：{p['title']}（全文已写入可恢复文件，工作区未动）", "project": p["id"], "recover": str(target), "recipients": list(members)})
            return {"deleted": p["id"], "recover": str(target)}
        if p.get("closed") and action not in {"project_close", "record_archive", "record_restore"}:
            # A closed project keeps its history readable and exportable, but no
            # action may add work, revoke decisions, or wake members again.
            raise Invalid("任务已结束：只能查看与导出，不能再派发、汇报或改动。")
        recipients = list(members)
        detail = {"recipients": recipients}
        result = p
        if action in {"record_archive", "record_restore"}:
            kind = a.get("kind")
            if kind not in {"project_message", "issue_message", "issue", "task"}:
                raise Invalid("请选择群聊消息、议题消息、议题或工作板块。")
            item_id = a.get("item_id")
            if kind == "project_message":
                item = get(p["chat"], item_id, "群聊消息")
            elif kind == "issue_message":
                issue = get(p["issues"], a.get("issue_id"), "议题")
                item = get(issue["messages"], item_id, "议题消息")
            elif kind == "issue":
                item = get(p["issues"], item_id, "议题")
            else:
                item = get(p["tasks"], item_id, "工作板块")
            archived = action == "record_archive"
            if bool(item.get("archived")) == archived:
                raise Invalid("记录已经处于所选状态，请刷新。")
            if kind == "task":
                if archived and item["status"] not in {"todo", "queued", "cancelled", "done"}:
                    raise Invalid("进行中或待核验板块先处理工作与核验，再回收记录。其待送达事件会同时撤销。")
                if archived and any(not i.get("archived") and i["status"] == "open" and item_id in i["task_ids"] for i in p["issues"]):
                    raise Invalid("板块关联未决议题，请先解决或回收议题。")
            if kind == "issue" and not archived:
                if any(get(p["tasks"], tid, "关联板块").get("archived") for tid in item["task_ids"]):
                    raise Invalid("请先恢复此议题关联的工作板块。")
            if kind == "issue_message" and not archived and issue.get("archived"):
                raise Invalid("请先恢复所属议题。")
            item["archived"] = archived
            item["archived_at"] = now() if archived else None
            if kind == "issue" and item["status"] == "open":
                for task in p["tasks"]:
                    if task["id"] not in item["task_ids"]:
                        continue
                    others = any(i is not item and i["status"] == "open" and not i.get("archived") and task["id"] in i["task_ids"] for i in p["issues"])
                    if archived and task["status"] == "blocked" and not others:
                        task["status"] = task.pop("before_block", "queued" if task["dispatched"] else "todo")
                    elif not archived and not others and task["status"] != "blocked":
                        task["before_block"] = task["status"]
                        task["status"] = "blocked"
            p["summary_requested"] = False
            detail.update(text=f"用户{'回收' if archived else '恢复'}了{kind}记录", kind=kind, item_id=item_id,
                          issue_id=a.get("issue_id") if kind == "issue_message" else item_id if kind == "issue" else None,
                          archived=archived, recipients=[])
            result = item
        elif action == "workspace_set":
            if p.get("workspace"):
                raise Invalid("任务已绑定工作区。更换目录请新建任务，避免混用已有交付依据。")
            if any(t["dispatched"] for t in p["tasks"]) and not p["paused"]:
                raise Invalid("请先暂停任务，再补充工作区。")
            p["workspace"] = self._workspace(a.get("workspace"), a.get("create_workspace") is True)
            detail.update(text="用户指定了工作区：" + p["workspace"])
        elif action == "demo_reset":
            if not p["demo"]:
                raise Invalid("只能重置演示任务。")
            p.update(paused=False, summary=None, summary_requested=False, closed=False)
            p["assignments"] = {"planner": "codex", "executor": "deepseek", "reporter": "antigravity"}
            self._demo(p)
            detail.update(text="已恢复演示任务的初始场景")
        elif action == "assign":
            self._assignments(state, a.get("assignments"))
            previous=p['assignments']['planner']
            if previous!=a['assignments']['planner']:
                p['review_epoch']=p.get('review_epoch',0)+1
                p['reviewer_history']=list(set(p.get('reviewer_history',[])) | {previous})
            p["assignments"] = a["assignments"]
            p["summary_requested"] = False
            detail.update(text="用户调整了职责分工", recipients=list(set(recipients) | set(a["assignments"].values())))
        elif action == "task_create":
            get(state["agents"], a.get("owner"), "负责人")
            task = {"id": uid("task"), "title": text(a.get("title"), "板块名称", 150), "acceptance": text(a.get("acceptance"), "验收条件", 3000), "owner": a["owner"], "status": "todo", "evidence": "", "review": "", "dispatched": False}
            p["tasks"].append(task)
            p["summary_requested"] = False
            p["summary"] = None
            detail.update(text=f"新增板块：{task['title']}", recipients=[task["owner"]])
            result = task
        elif action == "task_edit":
            t = get(p["tasks"], a.get("task_id"), "工作板块")
            if t["dispatched"]:
                raise Invalid("已派发的板块不能改写分工，请新增板块交接。")
            get(state["agents"], a.get("owner"), "负责人")
            t.update(title=text(a.get("title"), "板块名称", 150), acceptance=text(a.get("acceptance"), "验收条件", 3000), owner=a["owner"])
            result = t
            detail.update(text=f"更新板块：{t['title']}")
        elif action == "dispatch":
            if p["paused"]:
                raise Invalid("任务处于暂停派发状态。")
            if p.get("closed"):
                raise Invalid("任务已结束，不能再派发新工作。")
            if p["demo"]:
                raise Invalid("演示任务不会向真实应用派发，请先新建任务。")
            if not p.get("workspace") or not Path(p["workspace"]).is_dir():
                raise Invalid("请先指定一个可用的本地文件夹作为工作区。")
            pending = [t for t in p["tasks"] if not t["dispatched"] and not t.get("archived")]
            if not pending:
                raise Invalid("没有待派发的板块，请先添加。")
            for t in pending:
                t["dispatched"] = True
                t["status"] = "queued"
                self._event(db, p["id"], "task_queued", actor, {"text": f"等待应用领取：{t['title']}", "task_id": t["id"], "recipients": [t["owner"], p["assignments"]["planner"]]})
            detail.update(text=f"已加入信箱：{len(pending)} 个板块；等待应用领取")
        elif action == "pause":
            if not isinstance(a.get("paused"), bool):
                raise Invalid("暂停状态无效。")
            p["paused"] = a["paused"]
            detail.update(paused=p['paused'], text="已暂停派发；向成员发送协作暂停通知" if p["paused"] else "已恢复派发；向成员发送继续通知")
        elif action == "project_close":
            # "Summary submitted" and "user ends the project" are separate stages:
            # closing stops dispatch and revokes pending work first, then the
            # runtime archives panel-created sessions and only unbinds user-bound
            # ones. History stays readable and exportable.
            if p.get("closed"):
                raise Invalid("任务已经结束，无需重复结束。")
            days = a.get("retention_days", 7)
            if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
                raise Invalid("保留期请填写 1-365 天。")
            p.update(closed=True, closed_at=now(), paused=True, summary_requested=False, retention_days=days)
            cancelled = []
            for t in p["tasks"]:
                if t["status"] != "done":
                    t["status"] = "cancelled"
                    cancelled.append(t["title"])
            detail.update(
                text="用户结束了任务" + (f"，撤销 {len(cancelled)} 个未完成板块" if cancelled else "，所有板块已验收"),
                cancelled=cancelled, retention_days=days,
            )
            result = p
        elif action == "report":
            t = get(p["tasks"], a.get("task_id"), "工作板块")
            if actor != t["owner"]:
                raise Invalid("只能汇报自己负责的板块。")
            if t.get("archived"):
                raise Invalid("此工作板块已回收。")
            if p["paused"]:
                raise Invalid("用户已暂停，请停止推进。仍可发送议题消息。")
            if not t["dispatched"]:
                raise Invalid("用户尚未派发此板块。")
            if t["status"] == "done":
                raise Invalid("板块已经验收，不能覆盖结果。")
            status = a.get("status")
            if status not in {"running", "review"}:
                raise Invalid("状态仅支持 running 或 review；完成后交给监督者验收。")
            if any(t["id"] in i["task_ids"] and i["status"] == "open" and not i.get("archived") for i in p["issues"]):
                raise Invalid("该板块有未解决议题，请先协商。")
            evidence = text(a.get("evidence"), "进展与检查依据", 10000)
            t.update(status=status, evidence=evidence)
            detail.update(text=f"成员汇报：{t['title']}", task_id=t["id"], status=status, evidence=evidence, recipients=[p["assignments"]["planner"]])
            result = t
        elif action in {"accept", "review"}:
            t = get(p["tasks"], a.get("task_id"), "工作板块")
            if action=='review':
                if actor!=p['assignments']['planner'] and actor not in p.get('reviewer_history',[]):
                    raise Invalid('只有方案监督负责人可以核验。')
                epoch=a.get('review_epoch')
                if epoch is not None and (not isinstance(epoch,int) or isinstance(epoch,bool) or epoch<0):
                    raise Invalid('审核轮次无效。')
                if actor!=p['assignments']['planner'] or (epoch is not None and epoch!=p.get('review_epoch',0)):
                    reference={'actor':actor,'evidence':text(a.get('evidence'),'核验依据',10000),'created':now(),'reason':'审核负责人或轮次已变更，仅供参考'}
                    t.setdefault('review_history',[]).append(reference)
                    self._event(db,p['id'],'review_reference',actor,{'text':'旧审核结果已保留为参考，未改变当前验收状态','task_id':t['id']})
                    return {'reference_only':True,'task_id':t['id']}
                if p.get('review_epoch',0)>0 and epoch is None:
                    raise Invalid('审核负责人已变更，请先读取最新项目并携带 review_epoch 提交本轮核验。')
            if t.get("archived"):
                raise Invalid("此工作板块已回收。")
            if t["status"] != "review":
                raise Invalid("仅可核验已提交检查依据的板块。")
            if action == "review" and actor == t["owner"]:
                raise Invalid("不能核验自己执行的板块，请交给用户核验。")
            evidence = text(a.get("evidence"), "核验依据", 10000)
            approved = a.get("approved")
            if not isinstance(approved, bool):
                raise Invalid("请选择通过或返修。")
            t.update(status="done" if approved else "queued", review=evidence, reviewed_by=actor)
            p["summary_requested"] = False
            detail.update(text=f"{'验收通过' if approved else '交回返修'}：{t['title']}", evidence=evidence, recipients=[t["owner"], p["assignments"]["reporter"]])
            result = t
        elif action == "issue_create":
            p["summary_requested"] = False
            p["summary"] = None
            task_ids = a.get("task_ids", [])
            if not isinstance(task_ids, list):
                raise Invalid("关联板块无效。")
            for task_id in task_ids:
                t = get(p["tasks"], task_id, "关联板块")
                if t.get("archived"):
                    raise Invalid("关联板块已回收，请先恢复。")
                if t["status"] == "done":
                    raise Invalid("已验收板块不能被阻塞，请新增后续板块。")
            issue_members = a.get("members", list(p["assignments"].values()))
            if not isinstance(issue_members, list):
                raise Invalid("议题成员无效。")
            for member_id in issue_members:
                get(state["agents"], member_id, "议题成员")
            if actor != "user":
                issue_members.append(actor)
            issue_members = list(dict.fromkeys(issue_members + [p["assignments"]["planner"]]))
            options = a.get("options", [])
            if not isinstance(options, list) or len(options) > 4:
                raise Invalid("选项最多 4 项。")
            clean_options = [{"id": uid("option"), "title": text(o.get("title"), "选项", 150), "detail": text(o.get("detail"), "影响说明", 2000)} for o in options]
            if len(clean_options) == 1:
                raise Invalid("请提供至少两个选项，或只创建讨论。")
            body = text(a.get("body"), "问题说明", 10000)
            issue = {"id": uid("issue"), "title": text(a.get("title"), "议题名称", 150), "creator": actor, "members": issue_members, "task_ids": list(dict.fromkeys(task_ids)), "status": "open", "options": clean_options, "choice": None, "decision_note": "", "messages": [{"id": uid("message"), "actor": actor, "body": body, "created": now(), "attachments": []}], "created": now()}
            p["issues"].append(issue)
            for t in p["tasks"]:
                if t["id"] in task_ids:
                    if t["status"] != "blocked":
                        t["before_block"] = t["status"]
                    t["status"] = "blocked"
            detail.update(text=f"发起议题：{issue['title']}", issue_id=issue["id"], recipients=issue_members)
            result = issue
        elif action == "message":
            issue = get(p["issues"], a.get("issue_id"), "议题")
            if issue.get("archived"):
                raise Invalid("议题已回收，请先恢复后发言。")
            if actor != "user" and actor not in issue["members"]:
                raise Invalid("请先由用户邀请加入此议题。")
            body = text(a.get("body"), "消息", 12000)
            attachments = a.get("attachments", [])
            if not isinstance(attachments, list) or len(attachments) > 5:
                raise Invalid("一条消息最多 5 个附件。")
            clean = self._attachments(attachments)
            message = {"id": uid("message"), "actor": actor, "body": body, "created": now(), "attachments": clean}
            issue["messages"].append(message)
            detail.update(text=body[:300], issue_id=issue["id"], message_id=message["id"], recipients=[m for m in issue["members"] if m != actor])
            result = message
        elif action == "project_message":
            # Project-level chat exists independently of any issue, so a project
            # keeps one durable conversation even with no open topic.
            if p["demo"] and actor != "user":
                raise Invalid("演示任务不会请求真实成员。")
            if p.get("closed"):
                raise Invalid("任务已结束，项目群聊为只读记录。")
            body = text(a.get("body"), "消息", 12000)
            attachments = a.get("attachments", [])
            if not isinstance(attachments, list) or len(attachments) > 5:
                raise Invalid("一条消息最多 5 个附件。")
            clean = self._attachments(attachments)
            if actor != "user":
                latest_user = max((n for n, m in enumerate(p["chat"]) if m["actor"] == "user"), default=-1)
                if len(p["chat"]) - latest_user - 1 >= 4:
                    raise Invalid("等待用户回复后再继续发言，避免成员之间无限互发。")
            message = {"id": uid("message"), "actor": actor, "body": body, "created": now(), "attachments": clean}
            p["chat"].append(message)
            detail.update(text=body[:300], message_id=message["id"], recipients=[m for m in sorted(members) if m != actor], thread="project")
            result = message
        elif action == "invite":
            issue = get(p["issues"], a.get("issue_id"), "议题")
            if issue.get("archived"):
                raise Invalid("议题已回收，请先恢复。")
            member = get(state["agents"], a.get("agent_id"), "成员")
            if member["id"] not in issue["members"]:
                issue["members"].append(member["id"])
            detail.update(text=f"邀请 {member['name']} 加入议题", recipients=[member["id"]], issue_id=issue["id"])
        elif action == "resolve":
            issue = get(p["issues"], a.get("issue_id"), "议题")
            if issue.get("archived"):
                raise Invalid("议题已回收，请先恢复。")
            if issue["status"] != "open":
                raise Invalid("此议题已经处理，请刷新。")
            if issue["options"]:
                option = get(issue["options"], a.get("choice"), "选项")
                issue["choice"] = option["id"]
                note = f"采用：{option['title']}"
            else:
                note = "用户确认议题已解决"
            issue.update(status="resolved", decision_note=a.get("note", "").strip()[:6000], resolved=now())
            issue["messages"].append({"id": uid("message"), "actor": "user", "body": note + ("\n" + issue["decision_note"] if issue["decision_note"] else ""), "created": now(), "attachments": [], "decision": True})
            for t in p["tasks"]:
                if t["id"] in issue["task_ids"] and t["status"] == "blocked" and not any(other["status"] == "open" and not other.get("archived") and t["id"] in other["task_ids"] for other in p["issues"]):
                    t["status"] = t.pop("before_block", "queued" if t["dispatched"] else "todo")
            detail.update(text=note, issue_id=issue["id"], recipients=issue["members"])
            result = issue
        elif action == "summary_request":
            self._ready(p)
            if p["paused"]:
                raise Invalid("请先恢复派发。")
            if p["summary_requested"]:
                raise Invalid("已请求汇总，请等待负责人响应。")
            p["summary_requested"] = True
            detail.update(text="请整理方案、执行、核验与未完成事项", recipients=[p["assignments"]["reporter"]])
        elif action == "summary_submit":
            if actor != p["assignments"]["reporter"]:
                raise Invalid("只有当前汇总负责人可提交汇总。")
            self._ready(p)
            if not p["summary_requested"] or p["paused"]:
                raise Invalid("用户尚未派发汇总，或任务已暂停。")
            p["summary"] = {"body": text(a.get("body"), "汇总", 30000), "actor": actor, "created": now()}
            detail.update(text="汇总负责人提交了交付说明")
            result = p["summary"]
        else:
            raise Invalid("不支持的操作。")
        self._event(db, p["id"], action, actor, detail)
        return result

    def _attachments(self, attachments):
        clean = []
        for attachment in attachments:
            attachment_id = attachment.get("id", "") if isinstance(attachment, dict) else ""
            if not isinstance(attachment_id, str) or len(attachment_id) != 24 or any(c not in "0123456789abcdef" for c in attachment_id):
                raise Invalid("附件无效。")
            # Only uploaded files in this workspace may be attached.
            manifest = self.path.parent / "uploads" / f"{attachment_id}.json"
            if not manifest.is_file():
                raise Invalid("附件不存在，请重新上传。")
            clean.append(json.loads(manifest.read_text(encoding="utf-8")))
        return clean

    @staticmethod
    def _workspace(value, create=False):
        value = text(value, "工作区路径", 1000)
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise Invalid("请选择本地文件夹，或填写完整的绝对路径。")
        path = path.resolve()
        if not path.exists() and create:
            if not path.parent.is_dir():
                raise Invalid("新文件夹的上一级目录不存在，请先选择已有父文件夹。")
            path.mkdir()
        if not path.is_dir():
            raise Invalid("工作区文件夹不存在，请选择已有目录，或勾选创建新文件夹。")
        return str(path)

    @staticmethod
    def _assignments(state, assignments):
        if not isinstance(assignments, dict) or set(assignments) != {"planner", "executor", "reporter"}:
            raise Invalid("请指定方案监督、执行和汇总负责人。")
        for member_id in assignments.values():
            get(state["agents"], member_id, "负责人")

    @staticmethod
    def _ready(p):
        visible = [t for t in p["tasks"] if not t.get("archived")]
        if not visible or any(t["status"] != "done" for t in visible) or any(i["status"] == "open" and not i.get("archived") for i in p["issues"]):
            raise Invalid("请先完成所有板块核验并处理未决议题。")

    @staticmethod
    def _demo(p):
        p["tasks"] = [
            {"id": "demo_api", "title": "接口与数据层", "acceptance": "接口覆盖任务、成员与消息，提供检查记录。", "owner": "deepseek", "status": "blocked", "before_block": "running", "evidence": "演示数据：已完成接口能力检查，发现实时推送能力缺失。", "review": "", "dispatched": True},
            {"id": "demo_ui", "title": "界面布局", "acceptance": "按确认的参考图实现任务总览与群聊侧栏。", "owner": "deepseek", "status": "running", "evidence": "演示数据：任务总览布局进行中。", "review": "", "dispatched": True},
        ]
        p["issues"] = [{"id": "demo_issue", "title": "接口能力与原方案不一致", "creator": "deepseek", "members": ["codex", "deepseek"], "task_ids": ["demo_api"], "status": "open", "created": now(), "choice": None, "decision_note": "", "options": [{"id": "demo_a", "title": "使用轮询", "detail": "沿用现有接口，保持当前范围。"}, {"id": "demo_b", "title": "增加推送服务", "detail": "扩大范围，补充部署维护。"}], "messages": [
            {"id": "demo_m1", "actor": "deepseek", "body": "现有接口不支持实时推送。可以采用轮询，或增加推送服务，需要一起确认。", "created": now(), "attachments": []},
            {"id": "demo_m2", "actor": "codex", "body": "建议先用轮询完成当前版本。增加推送服务会扩大范围，交给你决定。", "created": now(), "attachments": []},
            {"id": "demo_m3", "actor": "user", "body": "先说明两种方案对交付的影响。", "created": now(), "attachments": []},
            {"id": "demo_m4", "actor": "codex", "body": "轮询可沿用现有接口；新增服务需要补充部署和维护。", "created": now(), "attachments": []},
        ]}]


def export_markdown(project, agents):
    names = {a["id"]: a["name"] for a in agents} | {"user": "我"}
    lines = [f"# {project['title']}", "", "> 演示任务，不代表真实执行。" if project["demo"] else "> 本地协作记录；成员汇报与核验依据分别保留。", "", "## 工作区", project.get("workspace") or "演示任务未绑定文件夹", "", "## 任务状态"]
    if project.get("closed"):
        lines.append(f"已结束（{project.get('closed_at') or '时间未知'}）；保留期 {project.get('retention_days') or 7} 天；群聊与交付记录为只读。")
    else:
        lines.append("进行中。")
    lines += ["", "## 目标", project["goal"], "", "## 分工"]
    for role, label in [("planner", "方案与监督"), ("executor", "执行"), ("reporter", "汇总")]:
        lines.append(f"- {label}：{names[project['assignments'][role]]}")
    lines += ["", "## 工作板块"]
    for t in project["tasks"]:
        if t.get("archived"):
            continue
        lines += [f"### {t['title']}", f"负责人：{names[t['owner']]}；状态：{t['status']}", f"验收条件：{t['acceptance']}", f"成员汇报：{t['evidence'] or '尚未提交'}", f"核验依据：{t['review'] or '尚未核验'}", ""]
    lines += ["## 项目群聊"]
    for m in project.get("chat", []):
        if m.get("archived"):
            continue
        lines += [f"- {names.get(m['actor'], m['actor'])}：{m['body']}"]
        lines += [f"  附件：{f['name']}" for f in m.get("attachments", [])]
    if not project.get("chat"):
        lines.append("尚无项目群聊记录。")
    lines += ["", "## 议题与决策"]
    for i in project["issues"]:
        if i.get("archived"):
            continue
        selected = next((o["title"] for o in i["options"] if o["id"] == i["choice"]), "未选择")
        lines += [f"### {i['title']}", f"状态：{i['status']}；选择：{selected}", i["decision_note"]]
        for m in i["messages"]:
            if m.get("archived"):
                continue
            lines += [f"- {names.get(m['actor'], m['actor'])}：{m['body']}"]
            lines += [f"  附件：{f['name']}" for f in m.get("attachments", [])]
        lines.append("")
    lines += ["## 汇总", project["summary"]["body"] if project["summary"] else "汇总负责人尚未提交。", ""]
    return "\n".join(lines)
