"""Original-planner review routing, accounting and stale-evidence protection."""
import copy
import json
import unittest

import test_relay as fixtures
from relaylib.contracts import RelayError
from relaylib.plugin import JobStore, expand_plan, usage_report
from relaylib.review import read_review, revision, save_review
from relaylib.runner import Runner, read_config
from relaylib.runtime import atomic_json


def review(status="approved"):
    return {"status": status, "summary": "已核对目标、执行回答与文件。", "evidence": ["已读取实际文件与检查结果。"],
            "issues": ["请修复 value.txt 后重新检查。"] if status == "changes_requested" else []}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config["review"] = True
        self.config = self.f.base / "config.json"
        atomic_json(self.config, self.f.config)

    def test_api_review_uses_original_planner_read_only_and_is_counted(self):
        output = fixtures.result()
        output["answer"] = "已修复文件。"
        runner, state = self.f.start([{"response": fixtures.plan()},
                                      {"response": output, "write": {"value.txt": "fixed"}},
                                      {"response": review()}])
        self.assertEqual(state["status"], "completed", state.get("failure"))
        self.assertEqual([r["body"]["model"] for r in self.f.upstream.requests],
                         ["planner-model", "worker-model", "planner-model"])
        self.assertEqual(self.f.upstream.requests[-1]["body"]["fixture_sandbox"], "read-only")
        self.assertEqual(runner.ledger.value["roles"]["planner"]["forwarded"], 2)
        saved = read_review(runner.run_dir, state)
        self.assertEqual(saved["reviewer"], "planner-model")
        self.assertFalse(saved["stale"])
        self.assertIn("已修复文件。", (runner.run_dir / "003-reviewer" / "input.txt").read_text(encoding="utf-8"))
        self.assertEqual(usage_report(runner.run_dir)["input_tokens"], 300)
        Runner(runner.config, runner.project, runner.run_dir).resume()
        self.assertEqual(len(self.f.upstream.requests), 3)

    def test_review_failure_does_not_erase_worker_answer_or_repeat_execution(self):
        output = fixtures.result()
        output["answer"] = "已完成，等待审核。"
        runner, state = self.f.start([{"response": fixtures.plan()},
                                      {"response": output, "write": {"value.txt": "fixed"}},
                                      {"exit_code": 3}, {"response": review("changes_requested")}])
        self.assertEqual(state["status"], "awaiting_recovery")
        self.assertEqual(state["last_result"]["answer"], output["answer"])
        state = Runner(runner.config, runner.project, runner.run_dir).resume(retry_interrupted=True)
        self.assertEqual(state["status"], "completed", state.get("failure"))
        self.assertEqual(read_review(runner.run_dir, state)["status"], "changes_requested")
        self.assertEqual(sum(r["body"]["model"] == "worker-model" for r in self.f.upstream.requests), 1)

    def test_caller_reviews_locally_once_and_stale_revision_is_rejected(self):
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [{"response": fixtures.result(), "write": {"value.txt": "fixed"}}]})
        store = JobStore(self.config, self.f.base / "jobs", [self.f.project])
        handoff = {"goal": "Fix value", "files": [{"path": "value.txt", "note": "Wrong value"}], "steps": ["Write fixed"]}
        job = store.submit(str(self.f.project), "review", handoff)
        row = store.wait(job["job_id"], 12, full_result=True)
        self.assertEqual(row["status"], "completed", row)
        self.assertIsNone(row["review"])
        saved = store.record_review(job["job_id"], row["review_revision"], review())
        self.assertEqual(saved["model_calls"], 0)
        self.assertEqual(len(self.f.upstream.requests), 1)
        invalid = {**review(), "resolved_residual": [0]}
        with self.assertRaises(RelayError):
            store.record_review(job["job_id"], row["review_revision"], invalid)
        run = store.directory(job["job_id"]) / "relay"
        state = json.loads((run / "state.json").read_text(encoding="utf-8"))
        state["last_result"]["answer"] = "changed result"
        atomic_json(run / "state.json", state)
        self.assertTrue(store.snapshot(job["job_id"])["review"]["stale"])
        with self.assertRaises(RelayError) as caught:
            store.record_review(job["job_id"], row["review_revision"], review())
        self.assertEqual(caught.exception.code, "review_stale")

    def test_review_without_project_tests_can_approve_response_task(self):
        self.f.config["checks"] = {}
        plan = fixtures.plan()
        plan["acceptance"] = []
        runner, state = self.f.start([{"response": plan}, {"response": fixtures.result()}, {"response": review()}])
        self.assertEqual(state["status"], "needs_manual_verification")
        self.assertEqual(read_review(runner.run_dir, state)["status"], "approved")
        self.assertIsNone(state["validation"])


if __name__ == "__main__":
    unittest.main()
