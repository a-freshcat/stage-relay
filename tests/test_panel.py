"""Observer verification uses fixtures and localhost only; no model API."""
import json
import shutil
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panel import ROOT, Store, handoff_input, make_server, public_events
from relaylib.runtime import atomic_json, run_process


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = ROOT / ".test-tmp"
        self.temp_root.mkdir(exist_ok=True)
        self.directory = self.temp_root / ("panel-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.run = self.directory / "task" / "relay"
        self.run.mkdir(parents=True)
        self.store = Store(self.directory)
        self.state = {"status": "running", "next": "worker", "pending": {"directory": "002-worker"},
                      "attempts": [{"role": "planner", "directory": "001-planner"}], "updated_at": 10,
                      "project": "fixture-project", "validation": None}
        atomic_json(self.run / "state.json", self.state)
        atomic_json(self.run / "config.json", {"codex_command": ["fake_codex.py"], "planner": {"model": "sol", "max_requests": 4}, "worker": {"model": "ds", "max_requests": 8}, "secret": "NEVER_EXPOSE_CONFIG"})
        atomic_json(self.run / "budget.json", {"roles": {"planner": {"forwarded": 3}, "worker": {"forwarded": 2}}})
        (self.run / "request.md").write_text("fixture request", encoding="utf-8")
        for name in ("001-planner", "002-worker"):
            phase = self.run / name
            phase.mkdir()
            prompt = 'USER REQUEST:\nfixture\nCONFIGURED CHECKS (ID -> argv):\n{}\nInstructions\n'
            if "worker" in name:
                prompt += 'CURRENT PLAN:\n{"goal":"original plan","steps":[]}\n'
            (phase / "input.txt").write_text(prompt, encoding="utf-8")
            (phase / "events.jsonl").write_text(json.dumps({"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "public output"}}) + "\n", encoding="utf-8")
        atomic_json(self.run / "001-planner" / "response.json", {"goal": "original plan"})

    def tearDown(self):
        target = self.directory.resolve()
        if not target.is_relative_to(self.temp_root.resolve()) or target.is_symlink():
            raise RuntimeError("Unsafe cleanup target")
        shutil.rmtree(target)

    def snapshot(self):
        return self.store.snapshot(self.store.runs()[0]["id"])

    def test_exact_receiver_input_not_latest_plan(self):
        atomic_json(self.run / "plan.json", {"goal": "later revised plan"})
        data = self.snapshot()
        self.assertEqual(data["handoffs"][1]["payload"]["plan"]["goal"], "original plan")
        self.assertEqual(data["handoffs"][1]["evidence"], "002-worker/input.txt")
        self.assertEqual(data["roles"]["worker"]["requests"], 2)
        self.assertNotIn("NEVER_EXPOSE_CONFIG", json.dumps(data))
        self.assertIsNone(self.store.snapshot("../config.json"))

    def test_live_refresh_and_partial_line(self):
        self.assertEqual(self.snapshot()["phases"][1]["status"], "running")
        events = self.run / "002-worker" / "events.jsonl"
        with events.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"item.completed","item":{"type":"agent_message","text":"later"}}\n{')
        self.assertEqual(self.snapshot()["phases"][1]["events"][-1]["text"], "later")
        self.state.update(status="awaiting_recovery", failure={"code": "timeout"})
        atomic_json(self.run / "state.json", self.state)
        self.assertEqual(self.snapshot()["phases"][1]["status"], "interrupted")

    def test_reasoning_filtered_and_tool_events_merged(self):
        path = self.run / "002-worker"
        rows = [{"type":"item.completed","item":{"id":"r","type":"reasoning","text":"PRIVATE_TRACE"}},
                {"type":"item.started","item":{"id":"c","type":"command_execution","command":"echo demo","status":"in_progress"}},
                {"type":"item.completed","item":{"id":"c","type":"command_execution","command":"echo demo","aggregated_output":"demo","exit_code":0,"status":"completed"}}]
        (path / "events.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        events, _ = public_events(path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["output"], "demo")
        self.assertNotIn("PRIVATE_TRACE", json.dumps(events))

    def test_replan_and_check_feedback(self):
        prefix = 'CONFIGURED CHECKS (ID -> argv):\n{}\nInstructions\n'
        payload = handoff_input(prefix + 'LAST RESULT (bounded handoff, not full history):\n{"status":"needs_replan","decisionNeeded":"D1"}\nINDEPENDENT CHECK RESULTS:\n[{"exit_code":1}]\n')
        self.assertEqual(payload["previous_result"]["decisionNeeded"], "D1")
        self.assertEqual(payload["checks"][0]["exit_code"], 1)
        prompt = 'USER REQUEST:\nCURRENT PLAN:\n{"fake":true}\n' + prefix
        self.assertEqual(handoff_input(prompt), {})

    def test_caller_plan_is_visible_without_another_planning_stage(self):
        self.state["plan_source"] = "caller"
        atomic_json(self.run / "state.json", self.state)
        data = self.snapshot()
        self.assertTrue(data["roles"]["planner"]["reused"])
        self.assertEqual(data["roles"]["planner"]["requests"], 0)
        self.assertEqual(data["handoffs"][1]["from"], "调用方 agent")

    def test_loopback_read_only_routes_and_no_mutation(self):
        before = {str(p): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        server = make_server(self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(server.url) as response:
                self.assertIn("Stage Relay", response.read().decode())
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            with urllib.request.urlopen(server.url + "icon.svg") as response:
                self.assertEqual(response.headers["Content-Type"], "image/svg+xml")
                self.assertEqual(response.read(), (ROOT / "assets/icon.svg").read_bytes())
            for route in ("api/runs", "api/run/" + self.store.runs()[0]["id"]):
                with urllib.request.urlopen(server.url + route) as response:
                    self.assertNotIn(b"NEVER_EXPOSE_CONFIG", response.read())
            for route, expected in (("../config.json", 404), ("api/run/../../config.json", 404)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(server.url + route)
                self.assertEqual(error.exception.code, expected)
                error.exception.close()
            request = urllib.request.Request(server.url + "api/runs", method="POST", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            request = urllib.request.Request(server.url + "api/runs", headers={"Host": "unrelated.example"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.run.rglob("*") if p.is_file()})

    def test_events_flush_before_process_exit(self):
        out, err = self.directory / "out.log", self.directory / "err.log"
        outcome = {}
        # A small flushed line must be visible while the child is still alive;
        # read(4096) with buffered file output used to hide it until exit.
        def run():
            outcome.update(run_process([sys.executable, "-u", "-c", "import time; print('visible-now', flush=True); time.sleep(3)"],
                                       self.directory, "", 8, 10_000, out, err))
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if out.exists() and b"visible-now" in out.read_bytes():
                    break
                time.sleep(0.03)
            self.assertIn(b"visible-now", out.read_bytes())
            self.assertTrue(thread.is_alive(), "The line should appear before the phase exits")
        finally:
            thread.join(timeout=10)
        self.assertEqual(outcome.get("exit_code"), 0)


if __name__ == "__main__":
    unittest.main()
