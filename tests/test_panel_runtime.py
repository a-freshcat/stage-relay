"""Panel reuse, stale-version notice and shortcut; local fixtures only, no models."""
import json
import os
import signal
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
from relaylib import panel_runtime
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json


class PanelRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.config = self.f.base / "config.json"
        atomic_json(self.config, self.f.config)
        self.jobs = JobStore(self.config, self.f.base / "jobs", [self.f.project])
        self.addCleanup(self._stop)

    def _stop(self):
        """Panels are detached launchers; end only the one this fixture started."""
        for root in {self.jobs.root, self.f.base / "jobs"}:
            record = root / ".panel.json"
            if not record.exists():
                continue
            try:
                pid = json.loads(record.read_text(encoding="utf-8"))["pid"]
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError, KeyError):
                pass

    def test_reuse_is_fast_and_same_instance(self):
        first = panel_runtime.open_panel(self.jobs)
        second = panel_runtime.open_panel(self.jobs)
        self.assertFalse(first["reused"]); self.assertTrue(second["reused"])
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(first["version"], panel_runtime.__version__)
        self.assertEqual(second["model_calls"], 0)
        total = {row["phase"]: row["ms"] for row in second["timings"]}
        self.assertLess(total["total"], 250, "reuse should not wait on a cold start")
        self.assertIn("health", total)
        self.assertNotIn("spawn", total)
        record = json.loads((self.jobs.root / ".panel.json").read_text(encoding="utf-8"))
        self.assertEqual(record["identity"]["version"], panel_runtime.__version__)

    def test_timings_are_recorded_but_never_mix_with_model_limits(self):
        panel_runtime.open_panel(self.jobs)
        panel_runtime.open_panel(self.jobs)
        rows = [json.loads(line) for line in (self.jobs.root / ".panel-runtime.json").read_text(encoding="utf-8").splitlines()]
        phases = {row["phase"] for row in rows}
        self.assertTrue({"read_record", "health", "total"} <= phases)
        self.assertTrue(all(type(row["ms"]) is float or type(row["ms"]) is int for row in rows))

    def test_identity_change_starts_a_new_panel_instead_of_reusing(self):
        first = panel_runtime.open_panel(self.jobs)
        jobs = JobStore(self.config, self.f.base / "jobs", [self.f.base])
        second = panel_runtime.open_panel(jobs)
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["url"], second["url"])
        third = panel_runtime.open_panel(jobs)
        self.assertTrue(third["reused"])
        self.assertEqual(third["url"], second["url"])

    def test_dead_record_is_replaced_quickly(self):
        panel_runtime.open_panel(self.jobs)
        record = self.jobs.root / ".panel.json"
        value = json.loads(record.read_text(encoding="utf-8"))
        value["instance"] = "stale-instance"
        record.write_text(json.dumps(value), encoding="utf-8")
        started = time.monotonic()
        result = panel_runtime.open_panel(self.jobs)
        self.assertFalse(result["reused"])
        self.assertLess(time.monotonic() - started, 8)
        live = json.loads(record.read_text(encoding="utf-8"))
        self.assertNotEqual(live["instance"], "stale-instance")

    def test_healthy_panel_from_another_version_is_reused_with_reconnect_notice(self):
        # Never start a second service for the same config; report the mismatch instead.
        first = panel_runtime.open_panel(self.jobs)
        record = self.jobs.root / ".panel.json"
        live = json.loads(record.read_text(encoding="utf-8"))
        live["identity"]["version"] = "0.0.9"
        record.write_text(json.dumps(live), encoding="utf-8")
        second = panel_runtime.open_panel(self.jobs)
        self.assertTrue(second["reused"])
        self.assertEqual(second["url"], first["url"])
        self.assertTrue(second["reconnect_required"])
        self.assertIn("0.0.9", second["reconnect_notice"])
        self.assertIn("重新连接", second["reconnect_notice"])
        self.assertEqual(second["version_mismatch"]["panel_version"], "0.0.9")

    def test_other_version_panel_triggers_reconnect_notice(self):
        panel_runtime.open_panel(self.jobs)
        record = self.jobs.root / ".panel.json"
        live = json.loads(record.read_text(encoding="utf-8"))
        # Simulate a healthy older panel recorded in the bounded history.
        atomic_json(self.jobs.root / ".panel-history.json", {})
        history = self.jobs.root / ".panel-history.jsonl"
        history.write_text("\n".join(json.dumps(row) for row in [
            {"version": "0.0.1", "url": live["url"], "pid": live["pid"],
             "instance": "other-instance", "time": time.time()}]) + "\n", encoding="utf-8")
        with patch.object(panel_runtime, "_health", side_effect=lambda url, instance: url if instance == "other-instance" else None):
            stale = panel_runtime._stale_panels(self.jobs)
        self.assertEqual(stale[0]["version"], "0.0.1")
        self.assertTrue(stale[0]["url"])

    def test_shortcut_is_local_and_not_autostart(self):
        path = panel_runtime.ensure_shortcut(self.jobs)
        self.assertTrue(Path(path).is_file())
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("--open-browser", text)
        self.assertIn(str(self.jobs.config), text)
        self.assertNotIn("schtasks", text.lower())
        self.assertNotIn("startup", text.lower())
        hint = panel_runtime.launch_hint(self.jobs)
        self.assertEqual(hint["model_calls"], 0)
        # Idempotent: a second call leaves the existing file alone.
        before = Path(path).read_bytes()
        self.assertEqual(panel_runtime.ensure_shortcut(self.jobs), path)
        self.assertEqual(Path(path).read_bytes(), before)

    def test_shortcut_updates_generated_old_version_only(self):
        path = Path(panel_runtime.ensure_shortcut(self.jobs))
        text = path.read_text(encoding="ascii")
        path.write_text(text.replace(str(panel_runtime.ROOT / "mcp_server.py"), "C:/old-plugin/mcp_server.py"), encoding="ascii")
        panel_runtime.ensure_shortcut(self.jobs)
        self.assertIn(str(panel_runtime.ROOT / "mcp_server.py"), path.read_text(encoding="ascii"))
        path.write_text("custom user shortcut", encoding="ascii")
        panel_runtime.ensure_shortcut(self.jobs)
        self.assertEqual(path.read_text(encoding="ascii"), "custom user shortcut")

    def test_launch_view_exposes_the_shortcut_without_calling_models(self):
        from panel import Store, make_server
        from relaylib.settings import Settings
        panel_runtime.ensure_shortcut(self.jobs)
        server = make_server(Store(self.jobs.root), settings=Settings(self.config), jobs=self.jobs)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with urllib.request.urlopen(server.url + "api/launch") as response:
            data = json.load(response)
        self.assertTrue(Path(data["shortcut"]).is_file())
        self.assertTrue(data["version"])
        self.assertEqual(self.f.upstream.requests, [])

    def test_cli_entry_prints_reusable_url(self):
        import subprocess
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "mcp_server.py"),
                   "--config", str(self.config), "--runs-root", str(self.jobs.root),
                   "--projects-root", str(self.f.project), "--open-panel"]
        # A parent's -X utf8 flag is not inherited by child Python processes.
        options = {"capture_output": True, "encoding": "utf-8", "timeout": 60,
                   "env": {**os.environ, "PYTHONUTF8": "1"}}
        first = json.loads(subprocess.run(command, **options).stdout.strip().splitlines()[-1])
        second = json.loads(subprocess.run(command, **options).stdout.strip().splitlines()[-1])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["url"], second["url"])
        self.assertEqual(second["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
