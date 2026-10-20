"""Request growth and local rejection diagnostics, using local fake endpoints."""
import json
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import test_relay as fixtures
from relaylib.contracts import RelayError
from relaylib.diagnostics import describe_failure
from relaylib.gateway import Gateway, Ledger
from relaylib.runner import ROOT, Runner, read_config
from relaylib.runtime import atomic_json
from panel import Store, public_events, request_activity


class RequestGrowthTests(unittest.TestCase):
    def request(self, gate, text):
        body = json.dumps({"model": "m", "input": text}).encode()
        req = urllib.request.Request(gate.base_url + "/responses", data=body,
                                     headers={"Authorization": "Bearer " + gate.token})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read()

    def test_default_accepts_grown_context_without_truncating_or_output_cap(self):
        with fixtures.WorkspaceTemp() as temp, fixtures.Upstream() as upstream:
            with Gateway({"url": upstream.url, "headers": {}}, "m", "worker", 3, "growth",
                         Ledger(Path(temp) / "budget.json"), Path(temp) / "audit") as gate:
                self.request(gate, "small")
                self.request(gate, "x" * 2_108_866)
            self.assertEqual(len(upstream.requests), 2)
            self.assertEqual(len(upstream.requests[1]["body"]["input"]), 2_108_866)
            self.assertNotIn("max_output_tokens", upstream.requests[1]["body"])

    def test_explicit_cap_rejection_does_not_reuse_previous_http_200(self):
        with fixtures.WorkspaceTemp() as temp, fixtures.Upstream() as upstream:
            with Gateway({"url": upstream.url, "headers": {}}, "m", "worker", 3, "growth",
                         Ledger(Path(temp) / "budget.json"), Path(temp) / "audit", max_body=100) as gate:
                self.request(gate, "small")
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.request(gate, "x" * 1000)
                caught.exception.close()
                self.assertEqual(gate.failure_context["stage"], "本地请求大小检查")
                self.assertEqual(gate.failure_context["local_status"], 413)
                self.assertFalse(gate.failure_context["request_forwarded"])
                self.assertGreater(gate.failure_context["body_bytes"], gate.failure_context["limit_bytes"])
                # The previous upstream response cannot describe a request never forwarded.
                self.assertIsNone(gate.upstream_status)
                diagnostic = describe_failure(RelayError("input_limit", ""), gate.failure_context)
                self.assertNotIn("upstream_status", diagnostic)
                self.assertEqual(diagnostic["limit_bytes"], 100)
                self.assertIn("未发送", diagnostic["message"])
            self.assertEqual(len(upstream.requests), 1)
            rows = [json.loads(line) for line in (Path(temp) / "audit").read_text().splitlines()]
            self.assertEqual(sum(row["event"] == "request_denied" for row in rows), 1)

    def test_config_default_and_null_remove_cap_but_explicit_values_stay(self):
        with fixtures.WorkspaceTemp() as temp:
            path = Path(temp) / "config.json"
            config = json.loads((ROOT / "config.example.json").read_text())
            for supplied, expected in (({}, None), ({"max_request_bytes": None}, None),
                                       ({"max_request_bytes": 2000000}, 2000000)):
                config["limits"] = supplied
                atomic_json(path, config)
                self.assertEqual(read_config(path)["limits"]["max_request_bytes"], expected)
            for value in (0, -1, True, "unlimited"):
                config["limits"] = {"max_request_bytes": value}
                atomic_json(path, config)
                with self.assertRaises(RelayError):
                    read_config(path)

    def test_visual_qa_is_optional_and_safe_without_blocking_delivery(self):
        with fixtures.WorkspaceTemp() as temp:
            root = Path(temp)
            config = json.loads((ROOT / "config.example.json").read_text())
            runner = Runner(config, root, root / "relay")
            runner.run_dir.mkdir()
            (runner.run_dir / "request.md").write_text("制作 SVG", encoding="utf-8")
            runner.state = {"plan_source": "caller"}
            prompt = runner._prompt("worker")
            for requirement in (
                "Visual inspection is optional support, not a delivery gate",
                "shell-launched browser is allowed when practical",
                "unique writable temporary user-data directory",
                "full stdout/stderr log files",
                "Never use --headless=old, --single-process, or --no-zygote",
                "return status completed with a concise residual note",
                "never use needs_replan or block solely for missing visual QA",
                "Do not build custom renderers",
                "Never claim visual verification without actually inspecting",
            ):
                self.assertIn(requirement, prompt)
    def test_relay_accepts_grown_request_and_preserves_worker_result(self):
        fixture = fixtures.RelayIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        entries = fixture.good_entries()
        entries[1].update(requests=2, request_input_bytes=[100, 2_108_866])
        runner, state = fixture.start(entries)
        self.assertEqual(state["status"], "completed")
        self.assertEqual((fixture.project / "value.txt").read_text(), "fixed")
        self.assertEqual(len(fixture.upstream.requests), 3)

    def test_relay_local_rejection_preserves_partial_work_and_exact_diagnostic(self):
        fixture = fixtures.RelayIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config["limits"]["max_request_bytes"] = 100000
        entries = fixture.good_entries()
        entries[1].update(requests=2, request_input_bytes=[100, 100100],
                          write_before_requests={"value.txt": "partial artifact"})
        runner, state = fixture.start(entries)
        failure = state["failure"]
        self.assertEqual(state["status"], "awaiting_recovery")
        self.assertEqual(failure["code"], "input_limit")
        self.assertEqual(failure["stage"], "本地请求大小检查")
        self.assertEqual(failure["local_status"], 413)
        self.assertEqual(failure["limit_bytes"], 100000)
        self.assertNotIn("upstream_status", failure)
        self.assertEqual((fixture.project / "value.txt").read_text(), "partial artifact")
        self.assertEqual(len(fixture.upstream.requests), 2)
        sequence = json.loads(fixture.sequence.read_text())
        sequence["entries"].append({"response": fixtures.result(), "write": {"value.txt": "fixed"}})
        atomic_json(fixture.sequence, sequence)
        runner.config["limits"]["max_request_bytes"] = None
        resumed = Runner(runner.config, fixture.project, runner.run_dir)
        self.assertEqual(resumed.resume(retry_interrupted=True)["status"], "completed")
        prompt = (runner.run_dir / "003-worker/input.txt").read_text(encoding="utf-8")
        self.assertIn("RECOVERY FROM PARTIAL WORK", prompt)
        self.assertIn("002-worker", prompt)
        self.assertNotIn("recovery", resumed.state)
        self.assertEqual(len(fixture.upstream.requests), 3)

    def test_monitor_reports_growth_without_forwarding_or_exposing_bodies(self):
        with fixtures.WorkspaceTemp() as temp:
            root = Path(temp)
            rows = [{"event": "request_forwarded", "body_bytes": 144789},
                    {"event": "request_forwarded", "body_bytes": 1926991},
                    {"event": "request_denied", "reason": "input_limit", "body_bytes": 2108866,
                     "limit_bytes": 2000000, "phase": "001-worker"}]
            (root / "requests.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            before = (root / "requests.jsonl").read_bytes()
            activity = request_activity(root, {"roles": {"worker": {"forwarded": 53}}})
            self.assertEqual(activity["forwarded_requests"], 53)
            self.assertEqual(activity["last_request_bytes"], 2108866)
            self.assertEqual(activity["first_request_bytes"], 144789)
            self.assertEqual(len(activity["warnings"]), 2)
            self.assertEqual((root / "requests.jsonl").read_bytes(), before)
            self.assertNotIn("input", activity)

    def test_browser_inventory_is_visible_without_publishing_mcp_documentation(self):
        with fixtures.WorkspaceTemp() as temp:
            root = Path(temp)
            event = {"type": "item.completed", "item": {"id": "browser", "type": "mcp_tool_call",
                     "server": "cua_repl", "status": "completed", "result": {"content": [
                         {"type": "text", "text": "PRIVATE_DOCUMENTATION"},
                         {"type": "text", "text": '{"apps":[],"browsers":[]}' }]}}}
            (root / "events.jsonl").write_text(json.dumps(event))
            events, _ = public_events(root)
            self.assertFalse(events[0]["browser_available"])
            self.assertIn("当前 Codex", events[0]["text"])
            self.assertNotIn("PRIVATE_DOCUMENTATION", str(events))

    def test_panel_corrects_old_local_failure_using_audit_without_editing_history(self):
        with fixtures.WorkspaceTemp() as temp:
            root = Path(temp)
            atomic_json(root / "state.json", {"status": "awaiting_recovery", "failure": {
                "code": "input_limit", "stage": "上游模型请求", "upstream_status": 200}})
            (root / "requests.jsonl").write_text(json.dumps({"event": "request_denied", "reason": "input_limit",
                                                          "body_bytes": 2108866, "limit_bytes": 2000000}))
            before = (root / "state.json").read_bytes()
            store = Store(run_dir=root)
            failure = store.snapshot(store.identity(root))["failure"]
            self.assertEqual(failure["body_bytes"], 2108866)
            self.assertEqual(failure["stage"], "本地请求大小检查")
            self.assertNotIn("upstream_status", failure)
            self.assertEqual((root / "state.json").read_bytes(), before)

    def test_local_file_input_limit_is_not_mislabelled_as_http_request(self):
        diagnostic = describe_failure(RelayError("input_limit", ""), {"stage": "准备模型阶段", "path": "request.md"})
        self.assertEqual(diagnostic["stage"], "准备模型阶段")
        self.assertNotIn("request_forwarded", diagnostic)
        self.assertNotIn("local_status", diagnostic)


if __name__ == "__main__":
    unittest.main()
