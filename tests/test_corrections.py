"""Review -> worker -> same reviewer, using local scripted CLI/model fixtures only."""
import copy
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

import test_relay as fixtures
from test_review import review
from test_plugin import handoff
from relaylib import corrections
from relaylib.contracts import RelayError
from relaylib.plugin import JobStore
from relaylib.review import read_review, revision
from relaylib.runner import Runner
from relaylib.runtime import atomic_json


def rejected():
    return {**review("changes_requested"), "change_requests": [{"path": "value.txt", "location": "第 1 行与完成说明",
        "problem": "完成说明缺少核对结果", "requested_change": "读取 value.txt 并说明实际内容",
        "verification": "文件内容应为 fixed；回答应说明实际核对结果"}]}


def output(answer="已核对 fixed"):
    return {**fixtures.result(), "answer": answer}


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config["review"] = True

    def test_reject_correct_checks_and_original_planner_reapprove(self):
        runner, state = self.f.start([{"response": fixtures.plan()},
            {"response": output("待核对"), "write": {"value.txt": "fixed"}},
            {"response": rejected()}, {"response": output()}, {"response": review()}])
        self.assertIsNone(state["failure"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["review_state"], "approved")
        self.assertEqual([r["body"]["model"] for r in self.f.upstream.requests],
                         ["planner-model", "worker-model", "planner-model", "worker-model", "planner-model"])
        worker_prompt = (runner.run_dir / "004-worker/input.txt").read_text(encoding="utf-8")
        self.assertIn("REVIEW CORRECTIONS", worker_prompt)
        self.assertIn("第 1 行与完成说明", worker_prompt)
        self.assertTrue(all(c["exit_code"] == 0 for c in state["validation"]))
        rounds = corrections.history(runner.run_dir)
        self.assertEqual(rounds[0]["status"], "approved")
        self.assertEqual(rounds[0]["worker_phase"], "004-worker")
        self.assertEqual(len(list((runner.run_dir / "reviews").glob("*.json"))), 2)
        self.assertIn("PREVIOUS REVIEW FEEDBACK", (runner.run_dir / "005-reviewer/input.txt").read_text(encoding="utf-8"))

    def test_selected_reviewer_is_pinned_across_new_result_revision(self):
        self.f.config["review"] = False
        self.f.config["planner"]["endpoint"] = {"base_url": "https://unused.invalid/v1", "api_key_env": "ABSENT_REVIEW_KEY"}
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [
            {"response": output("待核对"), "write": {"value.txt": "fixed"}},
            {"response": rejected()}, {"response": output()}, {"response": review()}]})
        runner = Runner(self.f.config, self.f.project, self.f.base / "selected")
        state = runner.start_with_plan(fixtures.plan())
        target = {"spec": {**copy.deepcopy(self.f.config["worker"]), "model": "saved-reviewer"},
                  "selection": {"provider_name": "已有接口", "model": "saved-reviewer"}, "result_revision": revision(runner.run_dir, state)}
        atomic_json(runner.run_dir / "reviewer-target.json", target)
        state = runner.review_result(target["result_revision"])
        self.assertIsNone(state["failure"], state.get("failure"))
        self.assertEqual([r["body"]["model"] for r in self.f.upstream.requests],
                         ["worker-model", "saved-reviewer", "worker-model", "saved-reviewer"])
        self.assertEqual(read_review(runner.run_dir, state)["reviewer"], "saved-reviewer")
        self.assertEqual(runner.ledger.value["roles"]["reviewer"]["forwarded"], 2)

    def test_caller_rejection_queues_worker_and_returns_new_version_to_caller(self):
        self.f.config.update(review=False, checks={})
        path = self.f.base / "config.json"
        atomic_json(path, self.f.config)
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [
            {"response": output("同一回答"), "write": {"value.txt": "fixed"}}, {"response": output("同一回答")}]})
        store = JobStore(path, self.f.base / "jobs", [self.f.project])
        self.addCleanup(lambda: [p.wait(timeout=15) for p in store.children])
        job = store.submit(self.f.project, "caller-correction", handoff())
        row = store.wait(job["job_id"], 15, full_result=True)
        saved = store.record_review(job["job_id"], row["review_revision"], rejected())
        self.assertTrue(saved["execution_queued"])
        corrected = store.wait(job["job_id"], 15, full_result=True)
        self.assertEqual(corrected["review_state"], "awaiting_caller")
        self.assertNotEqual(corrected["review_revision"], row["review_revision"])
        import sys
        from relaylib.runner import ROOT
        cli = subprocess.run([sys.executable, str(ROOT / "mcp_server.py"), "--config", str(path),
            "--runs-root", str(store.root), "--projects-root", str(self.f.project),
            "--wait-job", job["job_id"], "--seconds", "0", "--full-result"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["review_revision"], corrected["review_revision"])
        self.assertEqual([r["body"]["model"] for r in self.f.upstream.requests], ["worker-model", "worker-model"])
        with self.assertRaises(RelayError):
            store.record_review(job["job_id"], row["review_revision"], review())
        store.record_review(job["job_id"], corrected["review_revision"], review())
        final = store.snapshot(job["job_id"], full_result=True)
        self.assertFalse(final["review_cycle"]["active"])
        self.assertEqual(final["correction_history"][0]["status"], "approved")

    def test_repeated_identical_feedback_without_progress_pauses_and_can_continue(self):
        self.f.config["checks"] = {}
        plan = fixtures.plan(); plan["acceptance"] = []
        runner, state = self.f.start([{"response": plan},
            {"response": output("same"), "write": {"value.txt": "fixed"}}, {"response": rejected()},
            {"response": output("same")}, {"response": rejected()},
            {"response": output("new evidence")}, {"response": review()}])
        self.assertEqual(state["status"], "review_paused", state.get("failure"))
        self.assertEqual(state["review_state"], "paused_no_progress")
        self.assertEqual(len(self.f.upstream.requests), 5)
        before = runner.ledger.value["roles"]["worker"]["forwarded"]
        state = runner.correct_review(revision(runner.run_dir, state), allow_retry=True)
        self.assertEqual(state["review_state"], "approved", state.get("failure"))
        self.assertEqual(runner.ledger.value["roles"]["worker"]["forwarded"], before + 1)

    def test_user_pause_before_correction_preserves_feedback_and_resumes_same_round(self):
        class Paused(Runner):
            def _begin_correction(inner, *args, **kwargs):
                super()._begin_correction(*args, **kwargs)
                atomic_json(inner.run_dir / "correction-pause.json", {"requested": True})
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [{"response": fixtures.plan()},
            {"response": output("待核对"), "write": {"value.txt": "fixed"}}, {"response": rejected()},
            {"response": output()}, {"response": review()}]})
        runner = Paused(self.f.config, self.f.project, self.f.base / "paused")
        state = runner.start("Fix value")
        self.assertEqual(state["status"], "review_paused")
        self.assertEqual(len(self.f.upstream.requests), 3)
        self.assertEqual(state["correction_round"], 1)
        resumed = Runner(self.f.config, self.f.project, runner.run_dir)
        state = resumed.correct_review(revision(runner.run_dir, state), allow_retry=True)
        self.assertEqual(state["review_state"], "approved", state.get("failure"))
        self.assertEqual(state["correction_round"], 1)

    def test_insufficient_evidence_stops_without_execution_retry(self):
        runner, state = self.f.start([{"response": fixtures.plan()},
            {"response": output(), "write": {"value.txt": "fixed"}}, {"response": review("unable_to_verify")}])
        self.assertEqual(len(self.f.upstream.requests), 3)
        self.assertFalse(state["review_cycle"]["active"])
        self.assertEqual(read_review(runner.run_dir, state)["status"], "unable_to_verify")

    def test_pausing_is_local_and_can_be_requested_during_active_service_lock(self):
        from relaylib.runtime import FileLock
        path = self.f.base / "config.json"
        atomic_json(path, self.f.config)
        store = JobStore(path, self.f.base / "jobs", [self.f.project])
        with patch.object(store, "launch"):
            job = store.submit(self.f.project, "pause", handoff())
        directory = store.directory(job["job_id"])
        atomic_json(directory / "relay/state.json", {"review_cycle": {"active": True}})
        with FileLock(directory / "service.lock"):
            self.assertEqual(store.pause_corrections(job["job_id"])["status"], "pause_requested")
        self.assertEqual(self.f.upstream.requests, [])

    def test_local_checkpoint_sharing_error_retries_read_without_model_calls(self):
        from pathlib import Path
        from relaylib.contracts import load_json
        path = self.f.base / "checkpoint.json"
        atomic_json(path, {"ok": True})
        with patch.object(Path, "read_text", side_effect=[PermissionError(13, "fixture sharing lock"), '{"ok":true}']) as read:
            with patch("relaylib.contracts.time.sleep"):
                self.assertEqual(load_json(path), {"ok": True})
            self.assertEqual(read.call_count, 2)
        self.assertEqual(self.f.upstream.requests, [])

    def test_review_only_can_be_explicitly_continued_and_user_confirmation_ends_cycle(self):
        self.f.config.update(review=False, checks={})
        path = self.f.base / "config.json"
        atomic_json(path, self.f.config)
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [
            {"response": output("待核对"), "write": {"value.txt": "fixed"}}, {"response": output()}]})
        store = JobStore(path, self.f.base / "jobs", [self.f.project])
        self.addCleanup(lambda: [p.wait(timeout=15) for p in store.children])
        job = store.submit(self.f.project, "review-only", handoff())
        row = store.wait(job["job_id"], 15)
        saved = store.record_review(job["job_id"], row["review_revision"], rejected(), auto_correct=False)
        self.assertEqual(saved["model_calls"], 0)
        self.assertEqual(len(self.f.upstream.requests), 1)
        store.correct_result(job["job_id"], row["review_revision"])
        corrected = store.wait(job["job_id"], 15)
        self.assertEqual(corrected["review_state"], "awaiting_caller")
        store.confirm_result(job["job_id"], corrected["review_revision"])
        final = store.snapshot(job["job_id"])
        self.assertEqual(final["status"], "user_confirmed")
        self.assertFalse(final["review_cycle"]["active"])

    def test_automatic_rejection_requires_concrete_changes_before_queueing(self):
        from relaylib.review import validate_correction_review
        with self.assertRaises(RelayError):
            validate_correction_review(review("changes_requested"))
        validate_correction_review(rejected())
        self.assertEqual(self.f.upstream.requests, [])

    def test_actual_panel_displays_locations_verification_rounds_and_pause_actions(self):
        from relaylib.runner import ROOT
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const elements={};function node(tag,text,cls){return {tag,text:text||'',cls,dataset:{},children:[],append(...v){this.children.push(...v)},replaceChildren(){this.children=[]},addEventListener(){}}}
const $=id=>elements[id]||=node('div');const source=fs.readFileSync(process.argv[1],'utf8');
const ctx={node,$,checkPassed:c=>c.exit_code===0&&!c.failure,reviewLabel:s=>s};vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function changeRequestCard('),source.indexOf('function renderSubagents(')),ctx);
const data={state:'review_paused',review_state:'paused_no_progress',job_id:'job',review_revision:'rev',can_confirm:true,review_cycle:{active:false},
 correction_history:[{round:1,status:'paused_no_progress',review:{summary:'需要修改',change_requests:[{path:'a.py',location:'第 3 行',problem:'返回值错误',requested_change:'修正计算',verification:'运行已有测试，应返回 3'}]},answer:'已修正',checks:[{id:'test',exit_code:0}],final_review:{status:'changes_requested',summary:'仍有问题'}}]};
ctx.renderCorrections(data);
const text=JSON.stringify($('correction-rounds'));
for(const value of ['第 3 行','修正计算','运行已有测试','已修正','仍有问题'])assert.ok(text.includes(value),value);
assert.ok(JSON.stringify($('correction-actions')).includes('继续按审核意见修改'));
assert.ok($('correction-status').textContent.includes('已暂停'));
ctx.renderCorrections({...data,review_state:'correcting',can_confirm:false,review_cycle:{active:true}});
assert.ok(JSON.stringify($('correction-actions')).includes('完成当前阶段后暂停返修'));
'''
        result = subprocess.run([shutil.which("node") or "node", "-e", script, str(ROOT / "panel/panel.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
