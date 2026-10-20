"""Worker-initiated context handoffs use fresh stages, never repeat accepted work."""
import json
import io
import threading
import unittest
import urllib.error
import urllib.request

import test_relay as fixtures
from relaylib.contracts import RelayError
from relaylib.runner import Runner
from relaylib.runtime import atomic_json
from relaylib.plugin import JobStore
from relaylib.assistance import validate_control
from mcp_server import serve
from panel import Store, make_server
from test_plugin import handoff
from test_subagents import event


def control(action="compact", question=""):
    return {"relay_control": action, "summary": "value.txt 已写入 partial，仍需完成修改。",
            "completed": ["保留已有文件"], "next_steps": ["把 value.txt 改为 fixed 并检查"],
            "evidence": ["value.txt: partial"], "question": question}


class ContextHelpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RelayIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.run_dir = self.fixture.base / "context-run"
        self.runner = Runner(self.fixture.config, self.fixture.project, self.run_dir)

    def sequence(self, entries):
        atomic_json(self.fixture.sequence, {"cursor": 0, "entries": entries})

    def test_compact_starts_fresh_worker_and_keeps_edits_and_cumulative_usage(self):
        self.sequence([{"response": control(), "write": {"value.txt": "partial"}},
                       {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        state = self.runner.start_with_plan(fixtures.plan(), "修正 value.txt")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["compaction_count"], 1)
        self.assertEqual(len(state["attempts"]), 2)
        self.assertEqual(self.runner.ledger.value["roles"]["worker"]["forwarded"], 2)
        next_prompt = (self.run_dir / "002-worker/input.txt").read_text(encoding="utf-8")
        self.assertIn("partial", next_prompt)
        self.assertIn("修正 value.txt", next_prompt)
        self.assertEqual((self.fixture.project / "value.txt").read_text(), "fixed")

    def test_help_stops_requests_then_versioned_answer_resumes_same_worker(self):
        self.sequence([{"response": control("help", "请确认 value.txt 的目标值。"),
                        "write": {"value.txt": "partial"}},
                       {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        state = self.runner.start_with_plan(fixtures.plan())
        self.assertEqual(state["status"], "awaiting_help")
        self.assertEqual(len(self.fixture.upstream.requests), 1)
        revision = state["help_request"]["revision"]
        with self.assertRaises(RelayError):
            self.runner.answer_help("0" * 64, "应当写 fixed")
        self.assertEqual(len(self.fixture.upstream.requests), 1)
        state = self.runner.answer_help(revision, "应当写 fixed，完成后运行已配置检查。")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.fixture.upstream.requests), 2)
        prompt = (self.run_dir / "002-worker/input.txt").read_text(encoding="utf-8")
        self.assertIn("应当写 fixed", prompt)
        with self.assertRaises(RelayError):
            self.runner.answer_help(revision, "重复答复")

    def test_duplicate_compaction_pauses_for_help_instead_of_spinning(self):
        self.sequence([{"response": control()}, {"response": control()}])
        state = self.runner.start_with_plan(fixtures.plan())
        self.assertEqual(state["status"], "awaiting_help")
        self.assertEqual(len(self.fixture.upstream.requests), 2)
        self.assertEqual(state["help_request"]["reason"], "repeated_checkpoint")

    def test_help_cannot_resume_after_external_project_edit(self):
        self.sequence([{"response": control("help", "需要建议")}])
        state = self.runner.start_with_plan(fixtures.plan())
        (self.fixture.project / "value.txt").write_text("external")
        with self.assertRaises(RelayError) as caught:
            self.runner.answer_help(state["help_request"]["revision"], "继续")
        self.assertEqual(caught.exception.code, "project_changed")

    def test_windows_browser_risk_is_explicit_without_changing_sandbox(self):
        self.run_dir.mkdir()
        (self.run_dir / "request.md").write_text("制作 SVG", encoding="utf-8")
        self.runner.state = {}
        prompt = self.runner._prompt("worker")
        self.assertIn("relay-context", prompt)
        self.assertIn("Windows workspace-write", prompt)
        self.assertIn("platform_channel", prompt)
        self.assertIn("Do not launch Edge/Chrome from this sandbox", prompt)

    def test_completed_manual_subagents_are_not_forced_to_repeat_after_compaction(self):
        self.fixture.config["subagents"] = {"mode": "manual", "max_concurrent": 1,
            "tasks": [{"goal": "Read value", "access": "read", "files": ["value.txt"]}]}
        self.sequence([{"response": control(), "events": [event("spawn_agent"), event("wait"), event("close_agent")]},
                       {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        state = self.runner.start_with_plan(fixtures.plan())
        self.assertEqual(state["status"], "completed", state.get("failure"))
        self.assertTrue(state["manual_delegation_completed"])
        self.assertIn("do not respawn", (self.run_dir / "002-worker/input.txt").read_text(encoding="utf-8"))

    def test_invalid_control_does_not_drop_required_evidence_or_question(self):
        for bad in ({**control("help"), "question": " "},
                    {**control(), "next_steps": []}, {**control(), "reset_budget": True}):
            with self.subTest(bad=bad), self.assertRaises(RelayError):
                validate_control(bad)

    def test_report_repair_cannot_invent_a_context_handoff(self):
        self.sequence([{"invalid_json": True}, {"response": control("help", "不要在格式修复时发起新动作")},
                       {"response": control()}])
        state = self.runner.start_with_plan(fixtures.plan())
        self.assertEqual(state["status"], "awaiting_recovery")
        self.assertNotIn("help_request", state)

    def test_help_reply_does_not_reset_request_budget(self):
        self.fixture.config["worker"]["max_requests"] = 1
        self.sequence([{"response": control("help", "请确认下一步")}, {"response": fixtures.result()}])
        state = self.runner.start_with_plan(fixtures.plan())
        state = self.runner.answer_help(state["help_request"]["revision"], "继续处理原任务")
        self.assertEqual(state["status"], "awaiting_recovery")
        self.assertEqual(len(self.fixture.upstream.requests), 1)
        self.assertEqual(self.runner.ledger.value["roles"]["worker"]["forwarded"], 1)

    def paused_store(self):
        path = self.fixture.base / "job-config.json"
        atomic_json(path, self.fixture.config)
        self.sequence([{"response": control("help", "目标值是否为 fixed？")},
                       {"response": fixtures.result(), "write": {"value.txt": "fixed"}}])
        store = JobStore(path, self.fixture.base / "jobs", [self.fixture.project])
        self.addCleanup(lambda: [p.wait(timeout=20) for p in store.children])
        job = store.submit(str(self.fixture.project), "assistance", handoff(), review_mode="user_choice")
        row = store.wait(job["job_id"], 20)
        self.assertEqual(row["status"], "awaiting_help", row)
        return store, row

    def rpc(self, store, name, arguments):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}]
        reader = io.BytesIO(("\n".join(json.dumps(r) for r in requests) + "\n").encode())
        writer = io.BytesIO()
        serve(store, reader, writer)
        result = json.loads(writer.getvalue().splitlines()[-1])["result"]
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def test_mcp_wait_inbox_and_reply_complete_without_restarting_task(self):
        store, row = self.paused_store()
        pending = self.rpc(store, "relay_pending_help", {})["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(self.fixture.upstream.requests), 1)
        revision = row["help_request"]["revision"]
        self.assertIn(revision, pending[0]["caller_request"])
        reply = self.rpc(store, "relay_answer_help", {"job_id": row["job_id"], "help_revision": revision, "answer": "确认，写 fixed。"})
        self.assertEqual(reply["status"], "queued")
        done = store.wait(row["job_id"], 20)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(self.fixture.upstream.requests), 2)
        self.assertEqual(store.pending_help()["pending"], [])
        with self.assertRaises(RelayError):
            store.answer_help(row["job_id"], revision, "重复答复")

    def test_panel_help_is_visible_and_reply_route_preserves_same_origin_guard(self):
        store, row = self.paused_store()
        observer = Store(store.root)
        view = observer.snapshot(observer.identity(store.directory(row["job_id"]) / "relay"))
        self.assertTrue(view["can_answer_help"])
        self.assertIn("relay_answer_help", view["caller_help_request"])
        server = make_server(observer, 0, jobs=store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        payload = json.dumps({"job_id": row["job_id"], "help_revision": row["help_request"]["revision"], "answer": "写 fixed。"}).encode()
        url = server.url + "api/answer-help"
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}))
        self.assertEqual(error.exception.code, 403)
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}", "X-Relay-Panel": "settings"}
        with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers)) as response:
            self.assertEqual(json.load(response)["status"], "queued")
        self.assertEqual(store.wait(row["job_id"], 20)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
