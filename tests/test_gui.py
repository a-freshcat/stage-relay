"""GUI entry regressions: local HTTP/MCP only, never a paid model."""
import io
import json
import unittest
import threading
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
from panel import Store, make_server
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json
from relaylib.settings import Settings

from mcp_server import serve


class GuiEntryTests(unittest.TestCase):
    def test_plugin_exposes_a_panel_entry_before_task_submission(self):
        reader = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n'
                            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                            b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
        writer = io.BytesIO()
        serve(None, reader, writer)
        result = json.loads(writer.getvalue().splitlines()[-1])["result"]
        self.assertIn("relay_open_panel", {tool["name"] for tool in result["tools"]})


class GuiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = self.f.base / "config.json"
        atomic_json(self.config, self.f.config)
        atomic_json(self.f.sequence, {"cursor": 0, "entries": self.f.good_entries()})
        self.jobs = JobStore(self.config, self.f.base / "jobs", [self.f.project])
        self.addCleanup(lambda: [p.wait(timeout=20) for p in self.jobs.children])

    def server(self):
        server = make_server(Store(self.jobs.root), settings=Settings(self.config), jobs=self.jobs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def post(self, server, body, origin=None):
        request = urllib.request.Request(server.url + "api/jobs", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Relay-Panel": "settings",
                     "Origin": origin or server.url.split("/", 3)[0] + "//" + server.url.split("/", 3)[2]})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_gui_submit_runs_both_roles_and_shows_actual_handoff(self):
        server = self.server()
        for route in ("", "api/launch", "api/health", "api/runs"):
            with urllib.request.urlopen(server.url + route) as response:
                response.read()
        self.assertEqual(self.f.upstream.requests, [])
        body = {"project": str(self.f.project), "task_key": "gui-test", "request": "Fix value"}
        result = self.post(server, body)
        again = self.post(server, body)
        self.assertTrue(again["reused"])
        completed = self.jobs.wait(result["job_id"], 20)
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual([r["body"]["model"] for r in self.f.upstream.requests], ["planner-model", "worker-model"])
        self.assertTrue(all("max_output_tokens" not in r["body"] for r in self.f.upstream.requests))
        with urllib.request.urlopen(server.url + "api/run/" + result["run_id"]) as response:
            data = json.load(response)
        self.assertEqual({p["role"] for p in data["phases"]}, {"planner", "worker"})
        self.assertEqual(data["handoffs"][1]["payload"]["plan"]["goal"], "Fix value")
        self.assertEqual((self.f.project / "value.txt").read_text(), "fixed")

    def test_gui_rejects_cross_origin_and_unapproved_project(self):
        server = self.server()
        body = {"project": str(self.f.project), "task_key": "gui-test", "request": "Fix value"}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(server, body, "http://untrusted.invalid")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        body["project"] = str(self.f.base)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(server, body)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.f.upstream.requests, [])

    def test_missing_project_reports_actionable_error_before_model_calls(self):
        server = self.server()
        self.jobs.projects = [self.f.base]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(server, {"project": str(self.f.base / "deleted-demo"), "task_key": "missing", "request": "Try relay"})
        try:
            value = json.load(error.exception)
        finally:
            error.exception.close()
        self.assertEqual(value.get("code"), "project_missing")
        self.assertIn("目录", value["error"])
        self.assertEqual(self.f.upstream.requests, [])

    def test_recovery_before_first_checkpoint_keeps_full_workflow(self):
        with patch.object(self.jobs, "launch"):
            result = self.jobs.submit_task(str(self.f.project), "resume-full", "Fix value")
            directory = self.jobs.directory(result["job_id"])
            atomic_json(directory / "service.json", {"status": "stopped", "time": 0})
            self.jobs.resume(result["job_id"])
        operation = json.loads((directory / "operation.json").read_text())
        self.assertEqual(operation["action"], "plan_and_run")
        self.assertEqual(operation["request"], "Fix value")

    def test_panel_launcher_reuses_server_without_model_calls(self):
        import os
        import signal
        try:
            first = self.jobs.open_panel()
            second = self.jobs.open_panel()
            self.assertEqual(first["url"], second["url"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["model_calls"], 0)
            self.assertEqual(self.f.upstream.requests, [])
        finally:
            record = self.jobs.root / ".panel.json"
            if record.exists():
                os.kill(json.loads(record.read_text())["pid"], signal.SIGTERM)
                from relaylib.panel_runtime import _children
                for child in _children:
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
