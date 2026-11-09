import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from relay.domain import Invalid, Store, export_markdown


class Workflows(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parent.parent / "qa"
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="test-state-", dir=test_root)
        assert Path(self.temp.name).resolve().is_relative_to(test_root.resolve())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.p = self.store.apply("project_create", {"title": "测试任务", "goal": "交付一个可核验的结果", "workspace": self.temp.name})
        self.pid = self.p["id"]

    def act(self, action, actor="user", **args):
        return self.store.apply(action, {"project_id": self.pid, **args}, actor)

    def project(self):
        return self.store.snapshot()["projects"][0]

    def task(self, title="数据层", owner="deepseek"):
        return self.act("task_create", title=title, owner=owner, acceptance="检查命令成功且交付依据完整")

    def dispatched(self):
        t = self.task()
        self.act("dispatch")
        return t

    def review_ready(self):
        t = self.dispatched()
        self.act("report", "deepseek", task_id=t["id"], status="review", evidence="修改 api.py，执行 python -m unittest，6 个检查通过。")
        return t

    def issue(self, task_ids=None):
        return self.act("issue_create", "deepseek", title="接口受限", body="接口检查发现能力缺失", members=["codex", "deepseek"], task_ids=task_ids or [], options=[{"title":"轮询","detail":"保持范围"},{"title":"新增服务","detail":"增加部署工作"}])

    def test_dispatch_is_not_execution(self):
        t = self.dispatched()
        self.assertEqual(self.project()["tasks"][0]["status"], "queued")
        inbox = self.store.inbox("deepseek")
        self.assertTrue(any(e["kind"] == "task_queued" for e in inbox["events"]))
        self.assertEqual(inbox["projects"][0]["tasks"][0]["id"], t["id"])

    def test_only_owner_can_report(self):
        t = self.dispatched()
        with self.assertRaises(Invalid):
            self.act("report", "codex", task_id=t["id"], status="running", evidence="冒充执行者")
        self.assertEqual(self.project()["tasks"][0]["status"], "queued")

    def test_report_requires_dispatch_and_evidence(self):
        t = self.task()
        with self.assertRaises(Invalid):
            self.act("report", "deepseek", task_id=t["id"], status="running", evidence="进展")
        self.act("dispatch")
        with self.assertRaises(Invalid):
            self.act("report", "deepseek", task_id=t["id"], status="review", evidence=" ")

    def test_agent_cannot_self_accept_or_decide(self):
        t = self.review_ready()
        with self.assertRaises(Invalid):
            self.act("report", "deepseek", task_id=t["id"], status="done", evidence="自己说通过")
        with self.assertRaises(Invalid):
            self.act("accept", "deepseek", task_id=t["id"], approved=True, evidence="自己验收")
        i = self.issue()
        with self.assertRaises(Invalid):
            self.act("resolve", "codex", issue_id=i["id"], choice=i["options"][0]["id"])

    def test_only_affected_tasks_block_and_restore(self):
        first = self.task()
        self.task("界面布局")
        self.act("dispatch")
        self.act("report", "deepseek", task_id=first["id"], status="running", evidence="正在处理")
        i = self.issue([first["id"]])
        self.assertEqual([t["status"] for t in self.project()["tasks"]], ["blocked", "queued"])
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        self.assertEqual([t["status"] for t in self.project()["tasks"]], ["running", "queued"])
        with self.assertRaises(Invalid):
            self.act("resolve", issue_id=i["id"], choice=i["options"][1]["id"])

    def test_multiple_blockers_do_not_resume_early(self):
        t = self.dispatched()
        one = self.issue([t["id"]])
        two = self.issue([t["id"]])
        self.act("resolve", issue_id=one["id"], choice=one["options"][0]["id"])
        self.assertEqual(self.project()["tasks"][0]["status"], "blocked")
        self.act("resolve", issue_id=two["id"], choice=two["options"][0]["id"])
        self.assertEqual(self.project()["tasks"][0]["status"], "queued")

    def test_pause_notifies_and_prevents_new_work(self):
        t = self.dispatched()
        self.act("pause", paused=True)
        with self.assertRaises(Invalid):
            self.act("report", "deepseek", task_id=t["id"], status="running", evidence="继续做")
        with self.assertRaises(Invalid):
            self.act("dispatch")
        self.assertTrue(self.store.inbox("deepseek")["projects"][0]["paused"])
        self.act("pause", paused=False)
        self.act("report", "deepseek", task_id=t["id"], status="running", evidence="恢复后领取")

    def test_discussion_resolution(self):
        t = self.dispatched()
        i = self.act("issue_create", title="需要资料", body="先补充背景", task_ids=[t["id"]], options=[])
        self.act("resolve", issue_id=i["id"], note="资料已补齐")
        self.assertEqual(self.project()["tasks"][0]["status"], "queued")

    def test_membership_guards_group_messages(self):
        i = self.issue()
        with self.assertRaises(Invalid):
            self.act("message", "antigravity", issue_id=i["id"], body="尚未获邀")
        self.act("invite", issue_id=i["id"], agent_id="antigravity")
        self.act("message", "antigravity", issue_id=i["id"], body="我已加入")
        self.assertEqual(self.project()["issues"][0]["messages"][-1]["actor"], "antigravity")

    def test_independent_review_and_summary_handoff(self):
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=True, evidence="复跑 6 个检查通过，核对差异符合范围。")
        self.act("assign", assignments={"planner":"codex","executor":"deepseek","reporter":"codex"})
        self.act("summary_request")
        with self.assertRaises(Invalid):
            self.act("summary_submit", "antigravity", body="已被替换的汇总负责人")
        self.act("summary_submit", "codex", body="执行与核验均有记录，无未完成事项。")
        self.assertEqual(self.project()["summary"]["actor"], "codex")
        exported = export_markdown(self.project(),self.store.snapshot()["agents"])
        self.assertIn("复跑 6 个检查", exported)
        self.assertIn("无未完成事项", exported)

    def test_planner_cannot_review_own_work(self):
        t = self.task(owner="codex")
        self.act("dispatch")
        self.act("report", "codex", task_id=t["id"], status="review", evidence="执行者的结果")
        with self.assertRaises(Invalid):
            self.act("review", "codex", task_id=t["id"], approved=True, evidence="自己审核")
        self.act("accept", task_id=t["id"], approved=True, evidence="用户检查结果符合预期")

    def test_naming_one_member_does_not_wake_everyone(self):
        """The user's complaint: @Codex must not put the whole team to work."""
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "codex"})
        i = self.issue()
        self.act("message", "user", issue_id=i["id"], body="@codex 只看这一条")
        latest = [e for e in self.store.inbox("deepseek")["events"]
                  if e["kind"] == "message" and e["detail"].get("issue_id") == i["id"]]
        self.assertFalse(latest, "a message addressed to codex must not notify deepseek")
        targeted = [e for e in self.store.inbox("codex")["events"]
                    if e["kind"] == "message" and e["detail"].get("issue_id") == i["id"]]
        self.assertTrue(targeted)
        self.assertEqual(targeted[-1]["detail"]["recipients"], ["codex"])

    def test_no_mention_still_addresses_the_whole_thread(self):
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "codex"})
        i = self.issue()
        self.act("message", "user", issue_id=i["id"], body="大家都看一下这条")
        self.assertTrue([e for e in self.store.inbox("deepseek")["events"]
                         if e["kind"] == "message" and e["detail"].get("issue_id") == i["id"]])

    def test_an_unknown_mention_is_reported_instead_of_broadcast(self):
        i = self.issue()
        with self.assertRaises(Invalid) as raised:
            self.act("message", "user", issue_id=i["id"], body="@nobody 这条该给谁")
        self.assertIn("无法识别", str(raised.exception))

    def test_stage_summary_covers_one_issue_while_the_project_is_unfinished(self):
        # One board is done and reviewed; an unrelated board stays open, so a
        # project-wide summary must still be refused while the stage is not.
        done = self.review_ready()
        self.act("review", "codex", task_id=done["id"], approved=True, evidence="核验通过")
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "antigravity"})
        later = self.act("task_create", title="后续板块", acceptance="待做", owner="deepseek")
        self.act("dispatch", project_id=self.project()["id"])
        with self.assertRaises(Invalid):
            self.act("summary_request", "codex")
        # A discussion issue with no boards is a stage of its own once resolved.
        i = self.issue()
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        stage = self.act("summary_request", "codex", issue_id=i["id"])
        self.assertEqual(stage["status"], "requested")
        self.assertEqual(stage["revision"], 1)
        submitted = self.act("summary_submit", "antigravity",
                             stage_id=stage["id"], body="阶段完成说明")
        self.assertEqual(submitted["status"], "submitted")
        # The same revision is only accepted once.
        with self.assertRaises(Invalid):
            self.act("summary_submit", "antigravity", stage_id=stage["id"], body="重复提交")

    def test_stage_summary_waits_for_its_own_boards(self):
        task = self.dispatched()
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "antigravity"})
        i = self.issue(task_ids=[task["id"]])
        with self.assertRaises(Invalid) as raised:
            self.act("summary_request", "codex", issue_id=i["id"])
        self.assertIn("未验收", str(raised.exception))

    def test_supervisor_who_is_reporter_can_submit_directly(self):
        """The hinted path must actually work, not deadlock on a refused request."""
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "codex"})
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=True, evidence="核验通过")
        i = self.issue()
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        # Asking yourself is still refused at project scope, and the refusal says
        # which call to make instead.
        with self.assertRaises(Invalid) as asked:
            self.act("summary_request", "codex")
        self.assertIn("直接提交", str(asked.exception))
        # At stage scope the same caller is not refused: one call records the
        # stage, because a supervisor who reports has nobody to ask.
        with self.assertRaises(Invalid) as no_body:
            self.act("summary_request", "codex", issue_id=i["id"])
        self.assertIn("body", str(no_body.exception))
        stage = self.act("summary_request", "codex", issue_id=i["id"],
                         body="兼任者直接写下的阶段说明")
        self.assertEqual(stage["status"], "submitted")
        self.assertEqual(stage["body"], "兼任者直接写下的阶段说明")
        self.assertTrue(stage.get("self_submitted"))
        self.assertEqual(stage["revision"], 1)
        # The project-wide self-submit path still works and stays separate.
        summary = self.act("summary_submit", "codex", body="自行提交的最终说明")
        self.assertTrue(summary["body"])
        self.assertTrue(summary.get("self_submitted"))
        # Doing it twice without a new stage is not a second summary.
        with self.assertRaises(Invalid):
            self.act("summary_submit", "codex", body="重复提交")

    def test_a_stage_can_be_recorded_straight_from_its_issue(self):
        """The reporter must not need a request it cannot receive."""
        done = self.review_ready()
        self.act("review", "codex", task_id=done["id"], approved=True, evidence="核验通过")
        i = self.issue()
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        # The reporter names the issue itself, with no prior stage_id.
        stage = self.act("summary_submit", "codex", issue_id=i["id"], body="直接按议题汇总")
        self.assertEqual(stage["status"], "submitted")
        self.assertEqual(stage["issue_id"], i["id"])
        self.assertEqual(stage["revision"], 1)
        saved = self.project()["stages"]
        self.assertEqual([(s["issue_id"], s["status"]) for s in saved],
                         [(i["id"], "submitted")])
        # A second direct call must not invent revision 2 behind the panel's back.
        with self.assertRaises(Invalid) as twice:
            self.act("summary_submit", "codex", issue_id=i["id"], body="再来一次")
        self.assertIn("新的阶段请求", str(twice.exception))
        self.assertEqual(len(self.project()["stages"]), 1)
        # An open issue has no stage yet, so it is refused rather than recorded.
        still_open = self.issue()
        with self.assertRaises(Invalid):
            self.act("summary_submit", "codex", issue_id=still_open["id"], body="未解决")

    def test_a_report_to_the_user_is_counted_once_and_cleared_where_seen(self):
        """A report addressed to the user must be countable, per issue, and reset
        only where it was actually looked at."""
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "codex"})
        first = self.issue()
        second = self.issue()
        # Addressed to the user in three different ways that must all count.
        self.act("message", "deepseek", issue_id=first["id"], body="@user 交付依据已补齐")
        self.act("message", "deepseek", issue_id=first["id"], body="@我 请确认是否继续")
        self.act("message", "deepseek", issue_id=first["id"], body="汇报", mentions=["user"])
        # Not addressed: an ordinary member message and a code sample that only
        # talks about the token.
        self.act("message", "deepseek", issue_id=first["id"], body="这条是给 codex 的，@codex 请核验")
        self.act("message", "deepseek", issue_id=first["id"], body="写法示例：`@user` 不要触发")
        self.act("message", "codex", issue_id=second["id"], body="@用户 另一个议题的汇报")

        counts = self.project()["mention_counts"]
        self.assertEqual(counts.get(first["id"]), 3, counts)
        self.assertEqual(counts.get(second["id"]), 1, counts)
        self.assertEqual(self.project()["mention_total"], 4)

        # Clearing one issue leaves the other untouched.
        read = self.store.mark_user_mentions_read(self.pid, first["id"])
        self.assertEqual(len(read["cleared"]), 3)
        after = self.project()
        self.assertEqual(after["mention_counts"].get(first["id"], 0), 0)
        self.assertEqual(after["mention_counts"].get(second["id"]), 1)

        # Clearing a single message clears only that one.
        project_chat = self.act("project_message", "deepseek", body="@user 项目级汇报")
        self.assertTrue(project_chat["mentions_user"])
        self.assertEqual(self.project()["mention_counts"].get("project"), 1)
        self.store.mark_user_mentions_read(self.pid, None, [project_chat["id"]])
        self.assertEqual(self.project()["mention_counts"].get("project", 0), 0)

    def test_the_users_own_message_never_marks_itself_unread(self):
        i = self.issue()
        self.act("message", "user", issue_id=i["id"], body="@user 提醒我自己")
        self.assertEqual(self.project()["mention_total"], 0)

    def test_a_mention_inside_code_is_not_a_mention(self):
        """Talking about `@codex` must not route to codex, nor be refused."""
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "codex"})
        i = self.issue()
        inline = self.act("message", "user", issue_id=i["id"], body="我把 `@codex` 写成代码块")
        self.assertTrue(inline["body"])
        fenced = self.act("message", "user", issue_id=i["id"],
                          body="示例：\n```\n@codex 只是说明\n```\n结束")
        self.assertTrue(fenced["body"])
        events = [e for e in self.store.inbox("codex")["events"]
                  if e["kind"] == "message" and e["detail"].get("issue_id") == i["id"]]
        # Neither is addressed, so both take the ordinary whole-thread path.
        self.assertTrue(all(not e["detail"].get("mentions") for e in events), events)
        self.assertEqual(events[-1]["detail"]["recipients"], ["codex", "deepseek"])

    def test_a_completed_stage_is_summarisable_while_another_issue_is_open(self):
        done = self.review_ready()
        self.act("review", "codex", task_id=done["id"], approved=True, evidence="核验通过")
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "antigravity"})
        finished = self.issue()
        self.act("resolve", issue_id=finished["id"], choice=finished["options"][0]["id"])
        # A second issue stays open, so the project as a whole is not ready.
        still_open = self.issue()
        with self.assertRaises(Invalid):
            self.act("summary_request", "codex")
        stage = self.act("summary_request", "codex", issue_id=finished["id"])
        self.assertEqual(stage["issue_id"], finished["id"])
        self.act("summary_submit", "antigravity", stage_id=stage["id"], body="第一阶段")
        # The open issue blocks its own stage, not the finished one.
        with self.assertRaises(Invalid):
            self.act("summary_request", "codex", issue_id=still_open["id"])
        stages = self.project()["stages"]
        self.assertEqual([s["status"] for s in stages], ["submitted"])

    def test_supervisor_can_request_a_stage_summary(self):
        """The role that judges a stage finished is the role that must ask."""
        # Supervisor and reporter must be different people, otherwise the
        # supervisor would be asking itself; that case has its own test.
        self.act("assign", assignments={"planner": "codex", "executor": "deepseek",
                                        "reporter": "antigravity"})
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=True, evidence="核验通过")
        i = self.issue()
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        # planner is codex here, so the progress supervisor can raise it.
        self.act("summary_request", "codex")
        project = self.project()
        self.assertTrue(project["summary_requested"])
        # The reporter is woken, and the request records who asked.
        event = [e for e in self.store.inbox(project["assignments"]["reporter"])["events"]
                 if e["kind"] == "summary_request"]
        self.assertTrue(event)
        self.assertEqual(event[-1]["detail"].get("requested_by"), "codex")
        self.assertIn("阶段汇总", event[-1]["detail"].get("text", ""))

    def test_a_member_cannot_request_a_summary_out_of_role(self):
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=True, evidence="核验通过")
        i = self.issue()
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        # deepseek is the executor, not the supervisor and not the user.
        with self.assertRaises(Invalid):
            self.act("summary_request", "deepseek")

    def test_supervisor_who_is_also_reporter_is_told_to_submit_instead(self):
        reporter = self.project()["assignments"]["reporter"]
        self.act("assign", assignments={"planner": reporter, "executor": "deepseek",
                                        "reporter": reporter})
        t = self.review_ready()
        self.act("review", reporter, task_id=t["id"], approved=True, evidence="核验通过")
        i = self.issue()
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        with self.assertRaises(Invalid) as raised:
            self.act("summary_request", reporter)
        self.assertIn("直接提交", str(raised.exception))

    def test_summary_waits_for_checks_and_decisions(self):
        with self.assertRaises(Invalid):
            self.act("summary_request")
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=True, evidence="核验通过")
        i = self.issue()
        with self.assertRaises(Invalid):
            self.act("summary_request")
        self.act("resolve", issue_id=i["id"], choice=i["options"][0]["id"])
        self.act("summary_request")
        self.act("summary_submit", self.project()['assignments']['reporter'], body="整理结果")
        self.task("新增范围")
        self.assertIsNone(self.project()["summary"])
        self.assertFalse(self.project()["summary_requested"])

    def test_rework_requires_new_evidence(self):
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=False, evidence="缺少错误处理")
        self.assertEqual(self.project()["tasks"][0]["status"], "queued")
        self.assertTrue(any(e["kind"] == "review" for e in self.store.inbox("deepseek")["events"]))

    def test_transaction_rolls_back_invalid_issue(self):
        self.dispatched()
        before = self.store.snapshot()
        with self.assertRaises(Invalid):
            self.issue(["nonexistent"])
        self.assertEqual(before,self.store.snapshot())

    def test_concurrent_messages_and_restart_preserve_data(self):
        i = self.issue()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda n:self.act("message",issue_id=i["id"],body=f"并发消息 {n}"),range(15)))
        reopened = Store(self.store.path).snapshot()
        self.assertEqual(len(reopened["projects"][0]["issues"][0]["messages"]),16)
        sequences = [e["seq"] for e in reopened["events"]]
        self.assertEqual(len(sequences),len(set(sequences)))

    def test_demo_is_never_sent_to_real_agents(self):
        p = self.store.apply("demo_create",{})
        with self.assertRaises(Invalid):
            self.store.apply("dispatch",{"project_id":p["id"]})
        with self.assertRaises(Invalid):
            self.store.apply("message",{"project_id":p["id"],"issue_id":"demo_issue","body":"hello"},"deepseek")
        self.assertNotIn(p["id"],{p["id"] for p in self.store.inbox("deepseek")["projects"]})

    def test_cannot_attach_unregistered_files(self):
        i = self.issue()
        with self.assertRaises(Invalid):
            self.act("message",issue_id=i["id"],body="附件",attachments=[{"id":"a"*24,"name":"fake"}])

    def test_demo_reset_does_not_touch_real_work(self):
        with self.assertRaises(Invalid):
            self.act("demo_reset")
        demo = self.store.apply("demo_create", {})
        args = {"project_id":demo["id"],"issue_id":"demo_issue","choice":"demo_a"}
        self.store.apply("resolve", args)
        self.store.apply("demo_reset", {"project_id":demo["id"]})
        projects = self.store.snapshot()["projects"]
        self.assertEqual(projects[0]["id"], self.pid)
        self.assertEqual(projects[1]["issues"][0]["status"], "open")

    def test_workspace_is_required_and_can_be_created_explicitly(self):
        with self.assertRaises(Invalid):
            self.store.apply("project_create", {"title":"未选工作区","goal":"目标"})
        target=Path(self.temp.name)/"new-project-folder"
        with self.assertRaises(Invalid):
            self.store.apply("project_create", {"title":"待建目录","goal":"目标","workspace":str(target)})
        self.assertFalse(target.exists())
        project=self.store.apply("project_create", {"title":"新建目录","goal":"目标","workspace":str(target),"create_workspace":True})
        self.assertTrue(target.is_dir())
        self.assertEqual(project["workspace"],str(target.resolve()))
        self.assertEqual(self.store.inbox("deepseek")["projects"][-1]["workspace"],str(target.resolve()))

    def test_workspace_must_be_an_absolute_directory(self):
        for invalid in ["relative/folder",str(self.store.path)]:
            with self.assertRaises(Invalid):
                self.store.apply("project_create",{"title":"无效工作区","goal":"目标","workspace":invalid})


    def test_project_chat_exists_without_any_issue(self):
        self.assertEqual(self.project()["chat"], [])
        mine = self.act("project_message", body="没有议题，也要能交接进度")
        self.act("project_message", "deepseek", body="收到，先检查数据层")
        self.act("project_message", "codex", body="读回文件后再核验")
        chat = self.project()["chat"]
        self.assertEqual([m["actor"] for m in chat], ["user", "deepseek", "codex"])
        self.assertEqual(mine["body"], "没有议题，也要能交接进度")
        self.assertEqual(self.project()["issues"], [])

    def test_project_chat_rejects_members_outside_the_project(self):
        with self.assertRaises(Invalid):
            self.act("project_message", "antigravity", body="不属于此任务")
        with self.assertRaises(Invalid):
            self.act("project_message", body=" ")
        self.assertEqual(self.project()["chat"], [])

    def test_project_chat_limits_consecutive_member_replies(self):
        self.act("project_message", body="开始讨论")
        for n in range(4):
            self.act("project_message", "deepseek", body=f"第 {n} 条")
        with self.assertRaises(Invalid):
            self.act("project_message", "deepseek", body="第五条")
        self.act("project_message", body="用户插话")
        self.act("project_message", "deepseek", body="用户回复后继续讨论")
        self.assertEqual(self.project()["chat"][-1]["body"], "用户回复后继续讨论")

    def test_demo_project_chat_never_requests_real_members(self):
        demo = self.store.apply("demo_create", {})
        with self.assertRaises(Invalid):
            self.store.apply("project_message", {"project_id": demo["id"], "body": "hi"}, "deepseek")
        self.store.apply("project_message", {"project_id": demo["id"], "body": "演示群聊"})
        chat = next(p for p in self.store.snapshot()["projects"] if p["id"] == demo["id"])["chat"]
        self.assertEqual([m["actor"] for m in chat], ["user"])

    def test_legacy_state_gains_empty_project_chat(self):
        db = sqlite3.connect(self.store.path)
        raw = db.execute("SELECT body FROM workspace WHERE id=1").fetchone()[0]
        state = json.loads(raw)
        for project in state["projects"]:
            project.pop("chat", None)
        db.execute("UPDATE workspace SET body=?", (json.dumps(state, ensure_ascii=False),))
        db.commit()
        db.close()
        reopened = Store(self.store.path).snapshot()
        self.assertEqual(reopened["projects"][0]["chat"], [])
        self.act("project_message", body="旧任务也能开始项目群聊")
        self.assertEqual(len(self.project()["chat"]), 1)

    def test_export_includes_project_chat(self):
        self.act("project_message", body="项目群聊第一条记录")
        exported = export_markdown(self.project(), self.store.snapshot()["agents"])
        self.assertIn("## 项目群聊", exported)
        self.assertIn("项目群聊第一条记录", exported)


    def test_closing_a_project_stops_work_and_keeps_history(self):
        t = self.review_ready()
        self.act("review", "codex", task_id=t["id"], approved=True, evidence="核验通过")
        self.task("未完成板块")
        self.act("project_message", body="结束前最后一次交接")
        p = self.act("project_close", retention_days=3)
        self.assertTrue(p["closed"])
        self.assertTrue(p["paused"])
        self.assertEqual(p["retention_days"], 3)
        statuses = {task["title"]: task["status"] for task in p["tasks"]}
        self.assertEqual(statuses["未完成板块"], "cancelled")
        self.assertEqual(statuses["数据层"], "done")
        with self.assertRaises(Invalid):
            self.act("dispatch")
        with self.assertRaises(Invalid):
            self.act("report", "deepseek", task_id=t["id"], status="running", evidence="结束后继续")
        with self.assertRaises(Invalid):
            self.act("issue_create", "deepseek", title="新议题", body="结束后不再开议题")
        with self.assertRaises(Invalid):
            self.act("project_close", retention_days=3)
        with self.assertRaises(Invalid):
            self.act("project_message", body="结束后的消息")
        exported = export_markdown(self.project(), self.store.snapshot()["agents"])
        self.assertIn("已结束", exported)
        self.assertIn("结束前最后一次交接", exported)
        self.assertIn("未完成板块", exported)

    def test_close_requires_a_bounded_retention_period(self):
        for bad in [0, 366, True, "7", None]:
            with self.assertRaises(Invalid):
                self.act("project_close", retention_days=bad)
        self.assertFalse(self.project()["closed"])
        self.assertEqual(self.act("project_close", retention_days=7)["retention_days"], 7)


    def test_recycling_messages_issues_and_tasks_is_recoverable(self):
        task = self.dispatched()
        issue = self.act("issue_create", "deepseek", title="多开的议题", body="误开", task_ids=[task["id"]])
        self.act("project_message", body="要回收的群聊消息")
        message_id = self.project()["chat"][-1]["id"]
        issue_message_id = self.project()["issues"][0]["messages"][-1]["id"]

        self.act("record_archive", kind="project_message", item_id=message_id)
        self.act("record_archive", kind="issue_message", item_id=issue_message_id, issue_id=issue["id"])
        self.act("record_archive", kind="issue", item_id=issue["id"])
        self.act("record_archive", kind="task", item_id=task["id"])
        p = self.project()
        self.assertTrue(p["chat"][-1]["archived"])
        self.assertTrue(p["issues"][0]["archived"])
        self.assertTrue(p["tasks"][0]["archived"])
        self.assertEqual(p["tasks"][0]["status"], "queued")

        exported = export_markdown(p, self.store.snapshot()["agents"])
        self.assertNotIn("要回收的群聊消息", exported)
        self.assertNotIn("多开的议题", exported)

        with self.assertRaises(Invalid):
            self.act("message", "deepseek", issue_id=issue["id"], body="回收后不能发言")
        self.act("record_restore", kind="task", item_id=task["id"])
        self.assertFalse(self.project()["tasks"][0]["archived"])
        self.act("record_restore", kind="issue", item_id=issue["id"])
        self.assertEqual(self.project()["tasks"][0]["status"], "blocked")
        self.act("record_restore", kind="issue_message", item_id=issue_message_id, issue_id=issue["id"])
        self.act("record_restore", kind="project_message", item_id=message_id)
        p = self.project()
        self.assertFalse(p["chat"][-1]["archived"])
        self.assertFalse(p["issues"][0]["messages"][-1]["archived"])

    def test_recycle_refuses_live_work_and_guards_restore_order(self):
        task = self.dispatched()
        self.act("report", "deepseek", task_id=task["id"], status="running", evidence="正在执行")
        with self.assertRaises(Invalid):
            self.act("record_archive", kind="task", item_id=task["id"])
        issue = self.act("issue_create", "deepseek", title="阻塞中", body="先解决", task_ids=[task["id"]])
        self.act("record_archive", kind="issue_message", item_id=self.project()["issues"][0]["messages"][0]["id"], issue_id=issue["id"])
        self.act("record_archive", kind="issue", item_id=issue["id"])
        with self.assertRaises(Invalid):
            self.act("record_restore", kind="issue_message", item_id=self.project()["issues"][0]["messages"][0]["id"], issue_id=issue["id"])


    def test_forget_requires_closed_and_keeps_a_recoverable_copy(self):
        self.store.apply("demo_create", {})
        with self.assertRaises(Invalid):
            self.act("project_forget")
        self.act("task_create", "user", title="收尾板块", owner="deepseek", acceptance="结束即可")
        self.act("project_close", retention_days=3)
        result = self.act("project_forget")
        recover = Path(result["recover"])
        self.assertTrue(recover.is_file())
        saved = json.loads(recover.read_text(encoding="utf-8"))
        self.assertEqual(saved["id"], self.pid)
        self.assertEqual(saved["closed"], True)
        remaining = [p for p in self.store.snapshot()["projects"] if p["id"] == self.pid]
        self.assertEqual(remaining, [])



    def test_forget_is_reversible_from_the_recycle_area(self):
        self.store.apply("demo_create", {})
        self.act("task_create", "user", title="回收板块", owner="deepseek", acceptance="结束即可")
        self.act("project_close", retention_days=3)
        self.act("project_forget")
        self.assertEqual([p for p in self.store.snapshot()["projects"] if p["id"] == self.pid], [])
        result = self.store.apply("project_restore", {"project_id": self.pid}, "user")
        self.assertEqual(result["restored"], self.pid)
        back = next(p for p in self.store.snapshot()["projects"] if p["id"] == self.pid)
        self.assertTrue(back["closed"])
        self.assertEqual([t["title"] for t in back["tasks"]], ["回收板块"])
        with self.assertRaises(Invalid):
            self.store.apply("project_restore", {"project_id": self.pid}, "user")

if __name__ == "__main__":
    unittest.main()
