"""Guards for the context-growth optimisation.

Every test here protects a property the user asked for: a wake stays bounded as
the project ages, nothing readable is lost, and the member identity survives a
rebuilt session.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from relay.domain import Store, Invalid
from relay import brief, rules
from relay.runtime import Runtime

EVIDENCE = "检查命令通过；" * 200


class ContextGrowth(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent / "qa"
        self.temp = tempfile.TemporaryDirectory(prefix="test-state-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.runtime = Runtime(self.store)
        self.project = self.store.apply("project_create", {
            "title": "上下文", "goal": "验证唤醒大小", "workspace": self.temp.name})
        self.pid = self.project["id"]
        self.sessions = [{"id": "native-one", "workspace": self.temp.name, "status": "idle"}]
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})
        self.runtime.bind(self.pid, "deepseek", "native-one")

    def grow(self, tasks):
        for n in range(tasks):
            task = self.store.apply("task_create", {
                "project_id": self.pid, "title": f"板块 {n}",
                "acceptance": "检查命令成功", "owner": "deepseek"})
            self.store.apply("dispatch", {"project_id": self.pid})
            self.store.apply("report", {"project_id": self.pid, "task_id": task["id"],
                                        "status": "review", "evidence": f"{n}:{EVIDENCE}"}, "deepseek")
            self.store.apply("review", {"project_id": self.pid, "task_id": task["id"],
                                        "approved": True, "evidence": f"核验 {n}：{EVIDENCE}"}, "codex")

    def wake(self, body="看一眼"):
        self.store.apply("project_message", {"project_id": self.pid, "body": body})
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                if j["session"] == "native-one"]
        for job in jobs:
            self.runtime.receipt("deepseek", {"id": job["id"], "status": "delivered"})
        return jobs

    def drain(self, agent="deepseek", sessions=None):
        """Deliver everything already queued, so a measurement is a fresh wake."""
        sessions = sessions or self.sessions
        while True:
            jobs = [j for j in self.runtime.poll(agent, {"app": "dsh", "sessions": sessions})["jobs"]]
            if not jobs:
                return
            for job in jobs:
                self.runtime.receipt(agent, {"id": job["id"], "status": "delivered"})

    def test_report_and_review_wakes_preview_evidence_and_name_the_full_read(self):
        task = self.store.apply("task_create", {"project_id": self.pid, "title": "完整依据",
                                               "acceptance": "独立核验", "owner": "deepseek"})
        self.store.apply("dispatch", {"project_id": self.pid})
        # The supervisor's own binding receives the report it must judge.
        supervisor = [{"id": "native-critic", "workspace": self.temp.name, "status": "idle"}]
        self.runtime.poll("codex", {"app": "dsh", "sessions": supervisor})
        self.runtime.bind(self.pid, "codex", "native-critic")
        self.drain("codex", supervisor)
        self.store.apply("report", {"project_id": self.pid, "task_id": task["id"],
                                    "status": "review", "evidence": EVIDENCE}, "deepseek")
        job = [j for j in self.runtime.poll("codex", {"app": "dsh", "sessions": supervisor})["jobs"]
               if j["session"] == "native-critic"][0]
        # A longer preview than an ordinary wake, but never a completeness claim.
        self.assertGreater(len(job["brief"]["context"]["task"]["evidence"]), 400)
        self.assertLess(len(job["brief"]["context"]["task"]["evidence"]), len(EVIDENCE))
        self.assertIn("relay_task", job["prompt"])
        self.assertEqual(job["brief"]["context"]["task"]["full_evidence_via"], "relay_task")
        # And the complete text is really retrievable.
        self.assertEqual(self.store.read_task("codex", self.pid, task["id"])["evidence"], EVIDENCE)

    def test_wake_is_bounded_when_many_boards_are_open(self):
        for n in range(60):
            self.store.apply("task_create", {"project_id": self.pid, "title": f"开放 {n}",
                                            "acceptance": "读回", "owner": "deepseek"})
        job = self.wake("看规模")[0]
        summary = job["brief"]["project"]
        self.assertEqual(len(summary["tasks"]), brief.OPEN_TASK_LIMIT)
        self.assertEqual(summary["omitted"]["open_tasks_not_listed"], 60 - brief.OPEN_TASK_LIMIT)
        self.assertEqual(summary["counts"]["open_tasks"], 60)
        # The complete list is reachable without reading everything.
        page = self.store.read_list("deepseek", self.pid, "tasks", 0, 200)
        self.assertEqual(page["total"], 60)

    def test_read_list_pages_full_items(self):
        for n in range(5):
            self.store.apply("project_message", {"project_id": self.pid, "body": f"{n}:{EVIDENCE}"})
        page = self.store.read_list("deepseek", self.pid, "chat", 0, 2)
        self.assertEqual((page["total"], page["returned"], page["next_offset"]), (5, 2, 2))
        self.assertIn(EVIDENCE.strip(), page["items"][0]["body"])
        rest = self.store.read_list("deepseek", self.pid, "chat", page["next_offset"], 2)
        self.assertEqual(rest["offset"], 2)
        with self.assertRaises(Invalid):
            self.store.read_list("deepseek", self.pid, "unknown")
        with self.assertRaises(Invalid):
            self.store.read_list("antigravity", self.pid, "chat")
        with self.assertRaises(Invalid):
            self.store.read_list("deepseek", self.pid, "chat", limit=500)

    def test_wake_does_not_grow_with_finished_boards(self):
        self.wake("小项目")
        self.drain()
        small = len(self.wake("小项目二")[0]["prompt"].encode())
        self.grow(12)
        self.drain()
        large = len(self.wake("大项目")[0]["prompt"].encode())
        # Finished boards collapse to a count, so age adds no fixed cost. The
        # remaining difference is the id list of done boards, which is capped.
        self.assertLess(large - small, 900, (small, large))

    def test_finished_evidence_is_collapsed_but_never_lost(self):
        self.grow(6)
        job = self.wake("读一下")[0]
        self.assertNotIn(EVIDENCE.strip(), job["prompt"])
        brief = job["brief"]
        self.assertEqual(brief["project"]["omitted"]["done_tasks"], 6)
        # The same evidence is intact on demand.
        full = self.store.read_project("deepseek", self.pid)
        self.assertTrue(all(EVIDENCE.strip() in t["evidence"] for t in full["tasks"]))
        self.assertEqual(len(full["tasks"]), 6)

    def test_wake_names_the_event_and_its_object(self):
        task = self.store.apply("task_create", {"project_id": self.pid, "title": "定位",
                                               "acceptance": "读回", "owner": "deepseek"})
        self.store.apply("dispatch", {"project_id": self.pid})
        self.wake("派发确认")
        job = self.wake("再看")[0]
        self.assertIn(task["id"], job["prompt"])
        self.assertEqual(job["brief"]["event"]["kind"], "project_message")

    def test_inbox_default_is_incremental_and_full_history_is_explicit(self):
        self.store.apply("project_message", {"project_id": self.pid, "body": "一"})
        first = self.store.inbox("deepseek")
        self.assertTrue(first["events"])
        # Without an ack the window does not move: reading is not consuming.
        again = self.store.inbox("deepseek")
        self.assertEqual(again["from_seq"], 0)
        self.assertEqual([e["seq"] for e in again["events"]], [e["seq"] for e in first["events"]])
        self.store.apply("project_message", {"project_id": self.pid, "body": "二"})
        second = self.store.inbox("deepseek", ack=first["next_ack"])
        self.assertTrue(all(e["seq"] > first["cursor"] for e in second["events"]))
        self.assertLess(len(second["events"]), len(first["events"]) + 1)
        everything = self.store.inbox("deepseek", after=0)
        self.assertGreaterEqual(len(everything["events"]), len(first["events"]))

    def test_a_lost_inbox_response_does_not_lose_events(self):
        """Codex's repro: read, drop the reply, read again — nothing may vanish."""
        self.store.apply("project_message", {"project_id": self.pid, "body": "不能丢"})
        first = self.store.inbox("codex")
        self.assertTrue(any(e["kind"] == "project_message" for e in first["events"]))
        # The reply never reached the caller: it never got to send an ack.
        second = self.store.inbox("codex")
        self.assertEqual([e["seq"] for e in second["events"]], [e["seq"] for e in first["events"]])
        # A second bridge reading concurrently cannot consume it for the first.
        other = self.store.inbox("codex")
        self.assertEqual(len(other["events"]), len(first["events"]))
        # Only an explicit acknowledgement advances, and it advances exactly there.
        third = self.store.inbox("codex", ack=first["next_ack"])
        self.assertEqual(third["from_seq"], first["next_ack"])
        self.assertEqual(third["events"], [])
        self.assertEqual(self.store.inbox("codex")["from_seq"], first["next_ack"])

    def test_inbox_ack_cannot_move_backwards(self):
        self.store.apply("project_message", {"project_id": self.pid, "body": "一"})
        first = self.store.inbox("deepseek", ack=5)
        self.assertEqual(first["acked"], 5)
        # A stale or hostile lower acknowledgement is ignored, not honoured.
        back = self.store.inbox("deepseek", ack=1)
        self.assertEqual(back["acked"], 5)
        with self.assertRaises(Invalid):
            self.store.inbox("deepseek", ack=-1)
        with self.assertRaises(Invalid):
            self.store.inbox("deepseek", ack="9")

    def test_inbox_returns_summaries_not_full_projects(self):
        self.grow(5)
        inbox = self.store.inbox("deepseek", after=0)
        project = next(p for p in inbox["projects"] if p["id"] == self.pid)
        self.assertNotIn(EVIDENCE.strip(), json.dumps(project, ensure_ascii=False))
        self.assertEqual(project["counts"]["tasks"], 5)
        self.assertEqual(project["omitted"]["done_tasks"], 5)

    def test_first_read_never_skips_an_assignment_made_before_it(self):
        # Fill the event stream, then dispatch: a first read must still see it.
        for n in range(60):
            self.store.apply("project_message", {"project_id": self.pid, "body": f"噪音 {n}"})
        task = self.store.apply("task_create", {"project_id": self.pid, "title": "后派发",
                                               "acceptance": "读回", "owner": "deepseek"})
        self.store.apply("dispatch", {"project_id": self.pid})
        inbox = self.store.inbox("deepseek")
        self.assertEqual(inbox["from_seq"], 0)
        self.assertTrue(any(e["kind"] == "task_queued" and
                            e["detail"].get("task_id") == task["id"]
                            for e in inbox["events"]))

    def test_on_demand_reads_are_permission_checked_and_complete(self):
        self.grow(2)
        task = self.store.read_project("deepseek", self.pid)["tasks"][0]
        self.assertIn(EVIDENCE.strip(), self.store.read_task("deepseek", self.pid, task["id"])["evidence"])
        issue = self.store.apply("issue_create", {
            "project_id": self.pid, "title": "取舍", "body": EVIDENCE,
            "members": ["codex", "deepseek"],
            "options": [{"title": "A", "detail": "x"}, {"title": "B", "detail": "y"}]}, "deepseek")
        self.assertIn(EVIDENCE.strip(), self.store.read_issue("deepseek", self.pid, issue["id"])["messages"][0]["body"])
        with self.assertRaises(Invalid):
            self.store.read_project("antigravity", self.pid)
        with self.assertRaises(Invalid):
            self.store.read_task("deepseek", self.pid, "missing")

    def test_event_read_is_scoped_to_its_project(self):
        self.store.apply("project_message", {"project_id": self.pid, "body": "可追溯"})
        seq = max(e["seq"] for e in self.store.snapshot()["events"])
        event = self.store.read_event("deepseek", self.pid, seq)
        self.assertEqual(event["kind"], "project_message")
        other = self.store.apply("project_create", {
            "title": "别处", "goal": "g", "workspace": self.temp.name})
        with self.assertRaises(Invalid):
            self.store.read_event("deepseek", other["id"], seq)

    def test_rules_go_out_once_per_binding_then_only_a_digest(self):
        first = self.wake("第一次")[0]["prompt"]
        self.assertIn(rules.VERSION, first)
        self.assertIn("一、身份与工具", first)
        second = self.wake("第二次")[0]["prompt"]
        self.assertNotIn("一、身份与工具", second)
        self.assertIn("relay_rules", second)
        # The identical full text stays retrievable for a rebuilt session.
        self.assertEqual(rules.full("deepseek"), rules.full("deepseek"))
        self.assertIn("deepseek", rules.full("deepseek"))

    def test_a_changed_rules_version_is_resent_in_full(self):
        """A confirmed session must not keep stale rules after the text changes.

        Only the digest travels on ordinary wakes, so a session that already holds
        one version would never see a new section. The stored version is what makes
        the change visible instead of silently behind.
        """
        self.wake("先发一次")
        later = __import__("time").time() + 5
        with patch("relay.runtime.time.time", return_value=later):
            second = self.wake("同一版本再发")[0]["prompt"]
        self.assertNotIn("一、身份与工具", second)
        with patch.object(rules, "VERSION", "stage-relay/rules/v99"):
            sent = self.wake("规则改版后")[0]["prompt"]
        self.assertIn("一、身份与工具", sent)
        self.assertIn("stage-relay/rules/v99", sent)

    def test_rebinding_sends_the_rules_again(self):
        self.wake("绑定一")
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions + [
            {"id": "native-two", "workspace": self.temp.name, "status": "idle"}]})
        self.runtime.bind(self.pid, "deepseek", "native-two")
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions + [
            {"id": "native-two", "workspace": self.temp.name, "status": "idle"}]})["jobs"]
                if j["session"] == "native-two"]
        if not jobs:
            self.store.apply("project_message", {"project_id": self.pid, "body": "换绑后"})
            jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions + [
                {"id": "native-two", "workspace": self.temp.name, "status": "idle"}]})["jobs"]
                    if j["session"] == "native-two"]
        self.assertIn("一、身份与工具", jobs[0]["prompt"])

    def test_long_quiet_gap_resends_the_rules(self):
        self.wake("开始")
        later = time_now = __import__("time").time() + 7200
        with patch("relay.runtime.time.time", return_value=later):
            job = self.wake("两小时后")[0]
        self.assertIn("一、身份与工具", job["prompt"])

    def test_cursor_advances_only_for_scanned_events(self):
        self.store.apply("project_message", {"project_id": self.pid, "body": "起点"})
        self.wake("起点")
        with self.store.connect() as db:
            row = db.execute("SELECT seq FROM native_cursors WHERE project=? AND agent='deepseek'",
                             (self.pid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row[0], 1)
        self.wake("推进")
        with self.store.connect() as db:
            moved = db.execute("SELECT seq FROM native_cursors WHERE project=? AND agent='deepseek'",
                               (self.pid,)).fetchone()[0]
        self.assertGreater(moved, row[0])

    def test_upgrade_without_a_cursor_does_not_replay_delivered_history(self):
        # A database written by the previous version has jobs but no cursors.
        self.wake("旧版本的一轮")
        with self.store.connect() as db:
            db.execute("DELETE FROM native_cursors")
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                if j["session"] == "native-one"]
        self.assertEqual(jobs, [])
        # And the cursor is written so the next poll is incremental again.
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute(
                "SELECT seq FROM native_cursors WHERE project=? AND agent='deepseek'",
                (self.pid,)).fetchone())

    def test_discussion_bound_defers_an_event_instead_of_dropping_it(self):
        """The bound postpones one event; it must not be lost or block others."""
        for n in range(3):
            self.store.apply("project_message", {"project_id": self.pid, "body": f"成员 {n}"}, "codex")
            self.drain()
        # The fourth member reply exceeds the allowance and must not be delivered.
        self.store.apply("project_message", {"project_id": self.pid, "body": "成员第四条"}, "codex")
        self.assertEqual([j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                          if j["session"] == "native-one"], [])
        with self.store.connect() as db:
            deferred = [row[0] for row in db.execute(
                "SELECT seq FROM native_deferred WHERE project=? AND agent='deepseek'", (self.pid,))]
        self.assertEqual(len(deferred), 1, deferred)
        # A user message lifts the bound, so the postponed event finally lands.
        delivered = self.wake("用户回复")
        texts = " ".join(j["prompt"] for j in delivered)
        self.assertIn("成员第四条", texts)
        self.assertIn("用户回复", texts)
        with self.store.connect() as db:
            left = db.execute("SELECT COUNT(*) FROM native_deferred WHERE project=? AND agent='deepseek'",
                              (self.pid,)).fetchone()[0]
        self.assertEqual(left, 0)

    def test_one_issue_hitting_the_bound_does_not_block_another(self):
        """Codex's head-of-line case: a deferred issue A must not stop issue B."""
        bound = self.store.apply("issue_create", {
            "project_id": self.pid, "title": "议题A", "body": "A 讨论",
            "members": ["codex", "deepseek"]}, "codex")
        for n in range(4):
            self.store.apply("message", {"project_id": self.pid, "issue_id": bound["id"],
                                         "body": f"A 回复 {n}"}, "codex")
        self.drain()
        # A's fifth reply exceeds the bound and is postponed...
        self.store.apply("message", {"project_id": self.pid, "issue_id": bound["id"],
                                     "body": "A 第五条"}, "codex")
        # ...while a brand-new task for B still has to reach the member.
        other = self.store.apply("issue_create", {
            "project_id": self.pid, "title": "议题B", "body": "B 需要处理",
            "members": ["codex", "deepseek"]}, "codex")
        task = self.store.apply("task_create", {"project_id": self.pid, "title": "B 板块",
                                              "acceptance": "读回", "owner": "deepseek"})
        self.store.apply("dispatch", {"project_id": self.pid})
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                if j["session"] == "native-one"]
        texts = " ".join(j["prompt"] for j in jobs)
        self.assertIn(task["id"], texts, "a new task must not be blocked by issue A")
        self.assertIn("议题B", texts)
        self.assertNotIn("A 第五条", texts)
        with self.store.connect() as db:
            deferred_seqs = {row[0] for row in db.execute(
                "SELECT seq FROM native_deferred WHERE project=? AND agent='deepseek'", (self.pid,))}
        # Only A's fifth reply is postponed; A's own earlier replies were already
        # suppressed by the bound at creation time and never became deferrals.
        self.assertTrue(deferred_seqs)
        postponed = [seq for seq in deferred_seqs
                     if "A 第五条" in json.dumps(self.store.read_event("deepseek", self.pid, seq),
                                                 ensure_ascii=False)]
        self.assertEqual(len(postponed), 1, sorted(deferred_seqs))

    def test_cross_round_merge_unions_event_ids(self):
        for n in range(2):
            self.store.apply("project_message", {"project_id": self.pid, "body": f"第一批 {n}"})
        busy = [{**self.sessions[0], "status": "running"}]
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": busy})
        self.store.apply("project_message", {"project_id": self.pid, "body": "第二批"})
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": busy})
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                if j["session"] == "native-one"]
        self.assertEqual(len(jobs), 1, jobs)
        self.assertEqual(len(jobs[0]["merged_events"]), 2, jobs[0]["merged_events"])
        self.assertIn("第二批", jobs[0]["prompt"])
        # The base prompt is not duplicated by the second merge round.
        self.assertEqual(jobs[0]["prompt"].count("同一会话还有"), 1)

    def test_merge_drops_a_recycled_sibling_and_keeps_the_rest(self):
        first = self.store.apply("project_message", {"project_id": self.pid, "body": "头部"})
        middle = self.store.apply("project_message", {"project_id": self.pid, "body": "会被回收"})
        last = self.store.apply("project_message", {"project_id": self.pid, "body": "应保留"})
        busy = [{**self.sessions[0], "status": "running"}]
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": busy})
        # Recycle the middle event: it is merged, not the head.
        self.store.apply("record_archive", {"project_id": self.pid, "kind": "project_message",
                                           "item_id": middle["id"]})
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                if j["session"] == "native-one"]
        self.assertTrue(jobs, "the surviving events must still be delivered")
        self.assertIn("应保留", jobs[0]["prompt"])
        self.assertNotIn("会被回收", jobs[0]["prompt"])
        # The surviving ids are still listed, so nothing became untraceable.
        self.assertTrue(jobs[0].get("merged_events"))

    def test_cancelling_a_merged_head_revives_its_surviving_members(self):
        head = self.store.apply("project_message", {"project_id": self.pid, "body": "头部会被回收"})
        tail = self.store.apply("project_message", {"project_id": self.pid, "body": "被合并的仍有效"})
        busy = [{**self.sessions[0], "status": "running"}]
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": busy})
        self.store.apply("record_archive", {"project_id": self.pid, "kind": "project_message",
                                           "item_id": head["id"]})
        # The first poll cancels the head and revives the survivor; the second
        # delivers it, because the revived row is a fresh queued delivery.
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})
        jobs = [j for j in self.runtime.poll("deepseek", {"app": "dsh", "sessions": self.sessions})["jobs"]
                if j["session"] == "native-one"]
        self.assertTrue(jobs, "a surviving merged event must not die with its head")
        self.assertTrue(any("被合并的仍有效" in j["prompt"] for j in jobs),
                        [j["prompt"][:200] for j in jobs])
        with self.store.connect() as db:
            statuses = dict(db.execute("SELECT event,status FROM native_jobs WHERE agent='deepseek'").fetchall())
        self.assertEqual(statuses.get(2), "cancelled")
        self.assertIn(statuses.get(3), {"queued", "leased", "delivered"})

    def test_legacy_queued_prompt_is_migrated_in_place(self):
        # A real legacy prompt embedded the whole project, evidence included —
        # that is what made it big. Reproduce that shape, then migrate it.
        self.grow(6)
        self.store.apply("project_message", {"project_id": self.pid, "body": "旧式大提示"})
        busy = [{**self.sessions[0], "status": "running"}]
        self.runtime.poll("deepseek", {"app": "dsh", "sessions": busy})
        with self.store.connect() as db:
            job_id, event, raw = db.execute(
                "SELECT id,event,body FROM native_jobs WHERE status='queued' LIMIT 1").fetchone()
            legacy = json.loads(raw)
            legacy.pop("brief", None)
            legacy.pop("prompt_base", None)
            legacy["prompt"] = "旧版完整项目：" + json.dumps(
                self.store.snapshot()["projects"][0], ensure_ascii=False)
            legacy["merged_events"] = [event]
            db.execute("UPDATE native_jobs SET body=? WHERE id=?",
                       (json.dumps(legacy, ensure_ascii=False), job_id))
        # Opening the panel runs the migration once, at startup.
        Runtime(self.store)
        with self.store.connect() as db:
            after = json.loads(db.execute("SELECT body FROM native_jobs WHERE id=?", (job_id,)).fetchone()[0])
        self.assertIn("brief", after)
        self.assertLess(len(after["prompt"]), len(legacy["prompt"]))
        self.assertEqual(after["merged_events"], [event])
        self.assertTrue(after["migrated_from_legacy_prompt"])
        # Idempotent: a second startup changes nothing and reports nothing.
        self.assertEqual(Runtime(self.store).migrate_queued(), 0)
        self.drain()

    def test_legacy_migration_never_touches_delivered_or_uncertain_rows(self):
        self.store.apply("project_message", {"project_id": self.pid, "body": "已送达"})
        job = self.wake("已送达二")[0]
        self.runtime.receipt("deepseek", {"id": job["id"], "status": "delivered"})
        with self.store.connect() as db:
            db.execute("UPDATE native_jobs SET status='uncertain' WHERE id=?", (job["id"],))
        self.assertEqual(Runtime(self.store).migrate_queued(), 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM native_jobs WHERE id=?",
                                       (job["id"],)).fetchone()[0], "uncertain")

    def test_message_and_full_reads_are_available(self):
        message = self.store.apply("project_message", {"project_id": self.pid, "body": EVIDENCE})
        read = self.store.read_message("deepseek", self.pid, message["id"])
        self.assertEqual(read["body"], EVIDENCE)
        with self.assertRaises(Invalid):
            self.store.read_message("deepseek", self.pid, "missing")
        with self.assertRaises(Invalid):
            self.store.read_message("antigravity", self.pid, message["id"])
        # `full=True` is the explicit way to re-read everything from the start.
        self.store.inbox("deepseek")
        after_first = self.store.inbox("deepseek")
        everything = self.store.inbox("deepseek", full=True)
        self.assertEqual(everything["from_seq"], 0)
        self.assertGreaterEqual(len(everything["events"]), len(after_first["events"]))
        self.assertIn("next_after", everything["page"])
        self.assertIn("next_ack", everything)


if __name__ == "__main__":
    unittest.main()
