"""Plugin regressions use localhost fixtures only: no paid API/model calls."""
import copy
import io
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
from relaylib.contracts import RelayError
from relaylib.gateway import Gateway, Ledger
from relaylib.plugin import JobStore, TOOLS, concise_result, execution_config, expand_plan, usage_report
from relaylib.runner import ROOT, Runner, read_config
from relaylib.runtime import atomic_json, run_process
from mcp_server import serve
from configure_plugin import configure


def handoff():
    return {"goal": "Fix value", "files": [{"path": "value.txt", "note": "Current value is wrong"}],
            "steps": ["Write fixed into value.txt"], "decisions": ["Keep the file format"],
            "constraints": ["Change only value.txt"]}


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RelayIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.path = self.f.base / "config.json"
        atomic_json(self.path, self.f.config)

    def direct(self, entries, runner_class=Runner):
        atomic_json(self.f.sequence, {"cursor": 0, "entries": entries})
        atomic_json(self.path, self.f.config)
        runner = runner_class(execution_config(self.path), self.f.project, self.f.base / "runs" / "direct")
        state = runner.start_with_plan(expand_plan(handoff(), runner.config["checks"]))
        return runner, state

    def store(self, entries):
        atomic_json(self.f.sequence, {"cursor": 0, "entries": entries})
        store = JobStore(self.path, self.f.base / "jobs", [self.f.project])
        self.addCleanup(lambda: [p.wait(timeout=15) for p in store.children])
        return store

    def test_supplied_plan_never_resolves_missing_planner_credentials(self):
        self.f.config["planner"]["endpoint"] = {"base_url": "https://unused.invalid/v1", "api_key_env": "RELAY_ABSENT_PLANNER_KEY"}
        runner, state = self.direct([{"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual([r["body"]["model"] for r in self.f.upstream.requests], ["worker-model"])
        self.assertNotIn("max_output_tokens", self.f.upstream.requests[0]["body"])
        self.assertNotIn("planner", runner.ledger.value["roles"])
        prompt = (runner.run_dir / "001-worker" / "input.txt").read_text()
        self.assertIn("Change only value.txt", prompt)

    def test_design_block_returns_to_caller_then_resumes_with_same_budget(self):
        blocked = fixtures.result("needs_replan")
        blocked.update(failedStep="S1", decisionNeeded="D1", reason="Need another decision",
                       attempted=["Tried"], evidence=["Mismatch"])
        runner, state = self.direct([{"response": blocked}, {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "awaiting_plan")
        self.assertEqual(len(self.f.upstream.requests), 1)
        updated = handoff()
        updated["decisions"] = ["Use fixed as the expected value"]
        state = runner.resume_with_plan(expand_plan(updated, runner.config["checks"]))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(runner.ledger.value["roles"]["worker"]["forwarded"], 2)
        self.assertTrue(all(r["body"]["model"] == "worker-model" for r in self.f.upstream.requests))

    def test_recovered_worker_result_does_not_repeat_model_work(self):
        class Interrupted(Runner):
            def _accept_stage(self, *_):
                raise KeyboardInterrupt()
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [{"response": fixtures.result(), "write": {"value.txt": "fixed"}}]})
        runner = Interrupted(execution_config(self.path), self.f.project, self.f.base / "runs" / "recover")
        with self.assertRaises(KeyboardInterrupt):
            runner.start_with_plan(expand_plan(handoff(), runner.config["checks"]))
        state = Runner(runner.config, runner.project, runner.run_dir).resume()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.f.upstream.requests), 1)

    def test_small_original_limits_are_preserved(self):
        self.f.config["worker"]["max_requests"] = 2
        self.f.config["worker"]["max_output_tokens"] = 100
        atomic_json(self.path, self.f.config)
        config = execution_config(self.path)
        self.assertEqual(config["worker"]["max_requests"], 2)
        self.assertEqual(config["worker"]["max_output_tokens"], 100)
        self.assertEqual(config, read_config(self.path))

    def test_original_larger_limits_are_not_lowered_and_output_cap_is_not_injected(self):
        self.f.config["worker"]["max_requests"] = 80
        self.f.config["limits"].update(max_fix_rounds=2, max_request_bytes=2_000_000, max_result_bytes=200_000)
        atomic_json(self.path, self.f.config)
        config = execution_config(self.path)
        self.assertEqual(config, read_config(self.path))
        self.assertNotIn("max_output_tokens", config["worker"])

    def test_more_than_eight_requests_and_two_repairs_can_complete(self):
        runner, state = self.direct([{"response": fixtures.result(), "requests": 9},
                                     {"response": fixtures.result()},
                                     {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["fix_rounds"], 2)
        self.assertEqual(runner.ledger.value["roles"]["worker"]["forwarded"], 11)

    def test_revised_plan_cannot_reset_request_budget(self):
        self.f.config["worker"]["max_requests"] = 1
        blocked = fixtures.result("needs_replan")
        blocked.update(failedStep="S1", decisionNeeded="D1", reason="Need another decision",
                       attempted=["Tried"], evidence=["Mismatch"])
        runner, state = self.direct([{"response": blocked}, {"response": fixtures.result()}])
        self.assertEqual(state["status"], "awaiting_plan")
        state = runner.resume_with_plan(expand_plan(handoff(), runner.config["checks"]))
        self.assertEqual(state["failure"]["code"], "request_limit")
        self.assertEqual(len(self.f.upstream.requests), 1)

    def test_blocker_and_all_evidence_are_returned_without_saving_truncation(self):
        blocked = fixtures.result("needs_replan")
        blocked.update(changed=[{"path": "文" * 180, "summary": "改" * 160}] * 100,
                       residual=["余" * 180] * 30, failedStep="S1", decisionNeeded="D1",
                       reason="因" * 400, attempted=["试" * 200] * 30, evidence=["证" * 200] * 30)
        value = concise_result(blocked)
        self.assertEqual(value, blocked)

    def test_worker_does_not_disable_configured_plugins_or_mcp_tools(self):
        self.f.config["worker"]["timeout_seconds"] = 600
        with patch("relaylib.runner.run_process", wraps=run_process) as spy, patch("relaylib.runner.Gateway", wraps=Gateway) as gates:
            self.direct([{"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        argv = spy.call_args_list[0].args[0]
        self.assertFalse(any(s.startswith(("plugins.", "mcp_servers.")) for s in argv))
        self.assertEqual(gates.call_args.args[8], 600)

    def test_bad_fact_fails_before_any_call_or_checkpoint(self):
        bad = handoff()
        bad["files"][0]["path"] = "../outside.txt"
        runner = Runner(execution_config(self.path), self.f.project, self.f.base / "runs" / "bad")
        with self.assertRaises(RelayError):
            runner.start_with_plan(expand_plan(bad, runner.config["checks"]))
        self.assertFalse(runner.run_dir.exists())
        self.assertEqual(self.f.upstream.requests, [])

    def test_complete_handoff_over_old_twelve_kib_limit_is_preserved(self):
        detailed = handoff()
        detailed["steps"] = ["中" * 5000] * 20
        detailed["constraints"] = ["requirement " * 400]
        value = expand_plan(detailed, {})
        self.assertEqual([s["do"] for s in value["steps"]], detailed["steps"])
        self.assertEqual(value["decisions"][-1]["what"], detailed["constraints"][0])

    def test_structured_result_larger_than_old_thirty_two_kib_limit_is_accepted(self):
        output = fixtures.result()
        output["changed"] = [{"path": "value.txt", "summary": "necessary context " * 250}] * 10
        runner, state = self.direct([{"response": output, "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertGreater((runner.run_dir / "result.json").stat().st_size, 32000)
        self.assertEqual(state["last_result"], output)

    def test_check_feedback_is_not_shortened_and_full_logs_are_addressable(self):
        self.f.config["checks"]["test"] = [sys.executable, "-c", "from pathlib import Path; import sys; print('important-detail'+'x'*2000); sys.exit(0 if Path('value.txt').read_text()=='fixed' else 1)"]
        runner, state = self.direct([{"response": fixtures.result()}, {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertIn("important-detail", (runner.run_dir / "002-worker" / "input.txt").read_text())
        self.assertTrue(Path(state["validation"][0]["stdout_path"]).is_file())

    def test_full_result_can_be_read_losslessly_without_rerunning_model(self):
        output = fixtures.result()
        output["changed"][0]["summary"] = "necessary detail " * 300
        output["residual"] = ["residual evidence " * 200] * 5
        output["reason"] = "Necessary completion explanation"
        store = self.store([{"response": output, "write": {"value.txt": "fixed"}}])
        job = store.submit(str(self.f.project), "full-result", handoff())
        preview = store.wait(job["job_id"], 12)
        self.assertTrue(preview["result"]["details_truncated"])
        self.assertEqual(preview["result"]["residual"], output["residual"])
        self.assertEqual(preview["result"]["reason"], output["reason"])
        full = store.wait(job["job_id"], 0, after_revision=preview["revision"], full_result=True)
        self.assertEqual(full["result"], output)
        self.assertEqual(len(self.f.upstream.requests), 1)

    def test_old_job_resume_removes_legacy_saving_caps_without_resetting_ledger(self):
        blocked = fixtures.result("needs_replan")
        blocked.update(failedStep="S1", decisionNeeded="D1", reason="Need decision", attempted=["Tried"], evidence=["Failure"])
        store = self.store([{"response": blocked}, {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        job = store.submit(str(self.f.project), "legacy", handoff())
        self.assertEqual(store.wait(job["job_id"], 12)["status"], "awaiting_plan")
        directory = store.directory(job["job_id"])
        meta = json.loads((directory / "job.json").read_text())
        meta.pop("policy_version")
        atomic_json(directory / "job.json", meta)
        legacy = read_config(directory / "config.json")
        legacy["worker"].update(max_requests=8, max_output_tokens=2048)
        legacy["limits"].update(max_request_bytes=384000, max_result_bytes=32000, max_fix_rounds=1, max_replans=0)
        atomic_json(directory / "config.json", legacy)
        store.resume(job["job_id"], plan=handoff())
        done = store.wait(job["job_id"], 12)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["requests"]["worker"]["forwarded"], 2)
        migrated = read_config(directory / "config.json")
        self.assertEqual(migrated["worker"], read_config(self.path)["worker"])
        self.assertEqual(migrated["limits"], read_config(self.path)["limits"])
        self.assertTrue((directory / "config-before-quality-fix.json").exists())
        self.assertNotIn("max_output_tokens", self.f.upstream.requests[-1]["body"])

    def test_job_duplicate_and_unchanged_wait_make_no_extra_calls(self):
        store = self.store([{"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        job = store.submit(str(self.f.project), "fix-v1", handoff())
        duplicate = store.submit(str(self.f.project), "fix-v1", handoff())
        self.assertTrue(duplicate["reused"])
        done = store.wait(job["job_id"], seconds=12)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["usage"]["input_tokens"], 100)
        self.assertEqual(done["usage"]["cached_input_tokens"], 80)
        self.assertIsNone(done["usage"]["cost_usd"])
        self.assertNotIn("stdout_tail", json.dumps(done))
        self.assertFalse(store.wait(job["job_id"], 0, done["revision"])["changed"])
        self.assertEqual(store.resume(job["job_id"])["status"], "completed")
        self.assertEqual(len(self.f.upstream.requests), 1)
        changed = handoff()
        changed["goal"] = "A different task"
        with self.assertRaises(RelayError) as caught:
            store.submit(str(self.f.project), "fix-v1", changed)
        self.assertEqual(caught.exception.code, "task_key_conflict")

    def test_job_revised_plan_never_launches_planner(self):
        blocked = fixtures.result("needs_replan")
        blocked.update(failedStep="S1", decisionNeeded="D1", reason="Need decision", attempted=["Tried"], evidence=["Failure"])
        store = self.store([{"response": blocked}, {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        job = store.submit(str(self.f.project), "revised", handoff())
        row = store.wait(job["job_id"], 12)
        self.assertEqual(row["status"], "awaiting_plan", row)
        store.resume(job["job_id"], plan=handoff())
        row = store.wait(job["job_id"], 12)
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(row["requests"]["worker"]["forwarded"], 2)
        self.assertNotIn("planner", row["requests"])

    def test_project_allowlist_blocks_before_launch(self):
        store = self.store([])
        with self.assertRaises(RelayError) as caught:
            store.submit(str(self.f.base), "outside", handoff())
        self.assertEqual(caught.exception.code, "project_denied")
        self.assertEqual(store.children, [])

    def test_missing_usage_remains_unknown_including_failed_stages(self):
        directory = self.f.base / "accounting"
        atomic_json(directory / "001-worker" / "finished.json", {"usage": {"usage": [{"input_tokens": 1, "output_tokens": 2}]}})
        atomic_json(directory / "002-worker" / "finished.json", {"usage": {"usage": []}})
        value = usage_report(directory)
        self.assertIsNone(value["input_tokens"])
        self.assertIsNone(value["cached_input_tokens"])

    @unittest.skipUnless(os.name == "nt", "Windows atomic replacement sharing behavior")
    def test_checkpoint_survives_brief_observer_sharing_violation(self):
        target = self.f.base / "shared-state.json"
        atomic_json(target, {"status": "before"})
        original = os.replace
        calls = []

        def replace(source, dest):
            calls.append(dest)
            if len(calls) < 3:
                raise PermissionError(13, "Fixture reader holds the destination")
            original(source, dest)

        with patch("relaylib.runtime.os.replace", side_effect=replace):
            atomic_json(target, {"status": "completed"})
        self.assertEqual(json.loads(target.read_text())["status"], "completed")

    def test_stdio_protocol_launches_real_job_with_fake_models(self):
        store = self.store([{"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        # Real stdio process, with filesystem jobs and a subprocess worker behind it.
        import hashlib
        from relaylib.plugin import encoded
        job_id = hashlib.sha256(encoded({"project": os.path.normcase(str(self.f.project)), "task_key": "rpc-v1"}).encode()).hexdigest()[:24]
        detailed = handoff()
        detailed["steps"] = ["Retain this necessary implementation fact. " * 100] * 20
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "relay_submit", "arguments": {"project": str(self.f.project), "task_key": "rpc-v1", "plan": detailed}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "relay_wait", "arguments": {"job_id": job_id, "seconds": 12}}},
        ]
        process = subprocess.run([sys.executable, str(ROOT / "mcp_server.py"), "--config", str(self.path),
                                  "--runs-root", str(store.root), "--projects-root", str(self.f.project)],
                                 input="".join(json.dumps(r) + "\n" for r in requests).encode(), capture_output=True, timeout=25)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual({t["name"] for t in replies[1]["result"]["tools"]},
                         {"relay_open_panel", "relay_prepare", "relay_submit", "relay_wait", "relay_resume", "relay_record_review", "relay_correct", "relay_pause_corrections", "relay_pending_help", "relay_answer_help"})
        done = json.loads(replies[-1]["result"]["content"][0]["text"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(self.f.upstream.requests), 1)

    def test_startup_and_discovery_have_no_network_or_file_writes(self):
        store = self.store([])
        reader = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}\n'
                            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
        writer = io.BytesIO()
        with patch.object(store, "launch", side_effect=AssertionError("Unexpected launch")):
            serve(store, reader, writer)
        self.assertFalse(store.root.exists())
        self.assertEqual(self.f.upstream.requests, [])
        self.assertEqual(json.loads(writer.getvalue().splitlines()[0])["result"]["protocolVersion"], "2024-11-05")

    def test_generate_local_wiring_does_not_resolve_credentials(self):
        self.f.config["worker"]["endpoint"] = {"base_url": "https://unused.invalid/v1", "api_key_env": "RELAY_ABSENT_WORKER_KEY"}
        atomic_json(self.path, self.f.config)
        output = self.f.base / "mcp.json"
        configure(self.path, [self.f.project], output)
        wiring = json.loads(output.read_text())["mcpServers"]["stage-relay"]
        self.assertEqual(wiring["command"], sys.executable)
        self.assertIn(str(self.f.project), wiring["args"])
        self.assertEqual(self.f.upstream.requests, [])


class OutputGuardTests(unittest.TestCase):
    def request(self, gate, body, path="/responses"):
        req = urllib.request.Request(gate.base_url + path, data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + gate.token, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=5)

    def test_output_cap_clamps_before_forwarding(self):
        with fixtures.WorkspaceTemp() as temp, fixtures.Upstream() as upstream:
            with Gateway({"url": upstream.url, "headers": {}}, "m", "worker", 3, "cap", Ledger(Path(temp) / "budget.json"),
                         Path(temp) / "audit", max_output_tokens=256) as gate:
                with self.request(gate, {"model": "m", "max_output_tokens": 99999}):
                    pass
                with self.request(gate, {"model": "m", "max_output_tokens": 32}):
                    pass
            self.assertEqual([r["body"]["max_output_tokens"] for r in upstream.requests], [256, 32])

    def test_default_gateway_preserves_upstream_output_choice_and_compaction(self):
        with fixtures.WorkspaceTemp() as temp, fixtures.Upstream() as upstream:
            with Gateway({"url": upstream.url, "headers": {}}, "m", "worker", 2, "cap", Ledger(Path(temp) / "budget.json"),
                         Path(temp) / "audit") as gate:
                with self.request(gate, {"model": "m", "max_output_tokens": 16000}):
                    pass
                with self.request(gate, {"model": "m"}, "/responses/compact"):
                    pass
            self.assertEqual(upstream.requests[0]["body"]["max_output_tokens"], 16000)
            self.assertEqual(upstream.requests[1]["path"], "/v1/responses/compact")

    def test_oversized_request_is_denied_before_any_chargeable_call(self):
        with fixtures.WorkspaceTemp() as temp, fixtures.Upstream() as upstream:
            with Gateway({"url": upstream.url, "headers": {}}, "m", "worker", 2, "cap", Ledger(Path(temp) / "budget.json"),
                         Path(temp) / "audit", max_body=100, max_output_tokens=256) as gate:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request(gate, {"model": "m", "input": "x" * 1000})
                caught.exception.close()
            self.assertEqual(upstream.requests, [])


if __name__ == "__main__":
    unittest.main()
