"""Visible handoffs and startup diagnostics without paid models."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_relay as fixtures
from relaylib.runner import Runner,ROOT
from relaylib.runtime import atomic_json
from relaylib.plugin import JobStore, execute_job
from relaylib.diagnostics import describe_failure
from relaylib.contracts import RelayError
from panel import Store


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RelayIntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_project_lock_lives_outside_plugin_installation(self):
        runner=Runner(self.f.config,self.f.project,self.f.base/"run")
        self.assertFalse(runner.project_lock().path.is_relative_to(ROOT/".locks"))

    def test_different_jobs_and_plugin_versions_share_project_lock(self):
        first=Runner(self.f.config,self.f.project,self.f.base/"first")
        second=Runner(self.f.config,self.f.project,self.f.base/"second")
        with first.project_lock():
            with patch("relaylib.runner.ROOT",self.f.base/"another-version"):
                with self.assertRaises(RelayError) as caught:
                    with second.project_lock():
                        self.fail("Overlapping project writer")
        self.assertEqual(caught.exception.code,"busy")

    def test_title_uses_original_request_and_failed_start_remains_visible(self):
        root=self.f.base/"jobs";job=root/"failed";job.mkdir(parents=True)
        atomic_json(job/"job.json",{"project":str(self.f.project),"title":"测试一下能否实施接力","created_at":1})
        atomic_json(job/"operation.json",{"action":"plan_and_run","request":"测试一下能否实施接力"})
        atomic_json(job/"service.json",{"status":"stopped","error":"permission_denied","time":2})
        store=Store(root)
        rows=store.runs()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["title"],"测试一下能否实施接力")
        data=store.snapshot(rows[0]["id"])
        self.assertEqual(data["failure"]["code"],"permission_denied")
        self.assertEqual(data["phases"],[])
        self.assertEqual(data["project"],str(self.f.project))

    def test_permission_error_survives_service_and_panel_without_secret(self):
        config=self.f.base/"config.json";atomic_json(config,self.f.config)
        jobs=JobStore(config,self.f.base/"jobs",[self.f.project])
        with patch.object(jobs,"launch"):
            row=jobs.submit_task(str(self.f.project),"permission-case","修正数值并验证结果")
        with patch("relaylib.runner.FileLock",side_effect=PermissionError(13,"SECRET_API_KEY",str(self.f.base/"denied.lock"))):
            execute_job(jobs.directory(row["job_id"]))
        saved=jobs.snapshot(row["job_id"])
        self.assertEqual(saved["failure"],"permission_denied")
        self.assertEqual(saved["diagnostic"]["stage"],"获取项目锁")
        store=Store(jobs.root);data=store.snapshot(store.runs()[0]["id"])
        self.assertIn("项目锁",data["failure"]["action"])
        self.assertEqual(data["failure"]["errno"],13)
        self.assertNotIn("SECRET_API_KEY",json.dumps(data))
        self.assertEqual(self.f.upstream.requests,[])

    def test_language_rule_reaches_both_roles_without_extra_calls(self):
        atomic_json(self.f.sequence,{"cursor":0,"entries":self.f.good_entries()})
        config=self.f.base/"config.json";atomic_json(config,self.f.config)
        runner=Runner(self.f.config,self.f.project,self.f.base/"run")
        self.assertEqual(runner.start("修正数值并验证结果")["status"],"completed")
        for phase in ("001-planner","002-worker"):
            prompt=(runner.run_dir/phase/"input.txt").read_text(encoding="utf-8")
            self.assertIn("same language as USER REQUEST",prompt)
            self.assertIn("修正数值并验证结果",prompt)
        self.assertEqual(len(self.f.upstream.requests),2)

    def test_safe_endpoint_and_exception_fields_only(self):
        data=describe_failure(OSError(13,"SECRET"),{"stage":"连接模型接口","endpoint":"https://user:SECRET@example.test/v1?key=SECRET#SECRET","headers":{"Authorization":"SECRET"}})
        self.assertEqual(data["endpoint"],"https://example.test/v1")
        self.assertNotIn("SECRET",json.dumps(data))

    def test_legacy_caller_plan_uses_goal_as_title(self):
        run=self.f.base/"runs"/"old";run.mkdir(parents=True)
        atomic_json(run/"state.json",{"status":"completed"})
        atomic_json(run/"plan.json",{"goal":"修改登录页的按钮"})
        (run/"request.md").write_text("Execute the caller-supplied plan.",encoding="utf-8")
        store=Store(run_dir=run)
        self.assertEqual(store.runs()[0]["title"],"修改登录页的按钮")
