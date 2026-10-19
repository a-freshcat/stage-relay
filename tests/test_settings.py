"""Provider selection tests. No upstream HTTP/model calls."""
import copy
import json
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panel import ROOT, Store, make_server
from relaylib.runner import read_config
from relaylib.runtime import atomic_json
from relaylib.settings import Settings, SettingsError


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.parent = ROOT / ".test-tmp"
        self.parent.mkdir(exist_ok=True)
        self.root = self.parent / ("settings-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.config = self.root / "config.local.json"
        shutil.copyfile(ROOT / "config.example.json", self.config)
        self.settings = Settings(self.config)
        self.original = read_config(self.config)

    def tearDown(self):
        if not self.root.resolve().is_relative_to(self.parent.resolve()) or self.root.is_symlink():
            raise RuntimeError("Unsafe fixture cleanup")
        shutil.rmtree(self.root)

    def payload(self):
        view = self.settings.view()
        return {key: view[key] for key in ("revision", "profiles", "roles")}

    def custom(self):
        payload = self.payload()
        payload["profiles"].append({"id": "other", "name": "其他模型入口", "endpoint": {
            "base_url": "https://offline.example/v1", "api_key_env": "OTHER_MODEL_KEY"},
            "models": ["vendor/code-model"]})
        payload["roles"]["worker"] = {"provider_id": "other", "model": "vendor/code-model"}
        return payload

    def test_view_creates_no_files(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        view = self.settings.view()
        self.assertEqual(len(view["profiles"]), 1)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_custom_selection_persists_and_preserves_run_and_limits(self):
        run = self.root / "old-run"
        run.mkdir()
        shutil.copyfile(self.config, run / "config.json")
        frozen = (run / "config.json").read_bytes()
        updated = self.settings.save(self.custom())
        config = read_config(self.config)
        self.assertEqual(config["worker"]["model"], "vendor/code-model")
        self.assertEqual(config["worker"]["endpoint"]["api_key_env"], "OTHER_MODEL_KEY")
        for key in ("limits", "checks", "budget", "codex_command", "planner"):
            self.assertEqual(config[key], self.original[key])
        self.assertEqual((run / "config.json").read_bytes(), frozen)
        self.assertEqual(Settings(self.config).view()["roles"], updated["roles"])

    def test_stale_tab_cannot_overwrite_new_selection(self):
        stale = self.payload()
        self.settings.save(self.custom())
        with self.assertRaises(SettingsError) as error:
            self.settings.save(stale)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(read_config(self.config)["worker"]["model"], "vendor/code-model")

    def test_deleted_inactive_model_and_provider_stay_deleted_after_reload(self):
        view = self.settings.save(self.custom())
        payload = {key: view[key] for key in ("revision", "profiles", "roles")}
        original_profile = next(p for p in payload["profiles"] if p["id"] != "other")
        old_worker = self.original["worker"]["model"]
        original_profile["models"].remove(old_worker)
        view = self.settings.save(payload)
        self.assertNotIn(old_worker, next(p for p in view["profiles"] if p["id"] != "other")["models"])
        payload = {key: view[key] for key in ("revision", "profiles", "roles")}
        payload["roles"]["planner"] = {"provider_id": "other", "model": "vendor/code-model"}
        payload["profiles"] = [p for p in payload["profiles"] if p["id"] == "other"]
        self.settings.save(payload)
        reread = Settings(self.config).view()
        self.assertEqual([p["id"] for p in reread["profiles"]], ["other"])
        self.assertTrue(all(r["provider_id"] == "other" for r in reread["roles"].values()))

    def test_deleting_active_model_or_provider_requires_new_role_selection(self):
        original = self.config.read_bytes()
        for remove_provider in (False, True):
            payload = self.custom()
            if remove_provider:
                payload["profiles"] = [p for p in payload["profiles"] if p["id"] != "other"]
            else:
                payload["profiles"][-1]["models"] = []
            with self.assertRaises(SettingsError):
                self.settings.save(payload)
            self.assertEqual(self.config.read_bytes(), original)
            self.assertFalse(self.settings.profiles.exists())

    def test_invalid_endpoints_and_inline_secrets_are_rejected_before_write(self):
        original = self.config.read_bytes()
        bad_endpoints = [
            {"base_url": "http://remote.example/v1"},
            {"base_url": "https://user:secret@remote.example/v1"},
            {"base_url": "https://remote.example/v1?key=secret"},
            {"base_url": "https://remote.example/v1", "api_key": "DO_NOT_STORE"},
            {"base_url": "https://remote.example/v1", "api_key_env": "sk-DO_NOT_STORE"},
            {"base_url": "https://remote.example/v1", "native_auth": True},
            {"base_url": "https://remote.example/v1/chat/completions"},
        ]
        for ep in bad_endpoints:
            with self.subTest(endpoint=ep):
                payload = self.custom()
                payload["profiles"][-1]["endpoint"] = ep
                with self.assertRaises(SettingsError):
                    self.settings.save(payload)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse(self.settings.profiles.exists())

    def test_duplicate_profile_and_malformed_local_presets(self):
        payload = self.payload()
        duplicate = copy.deepcopy(payload["profiles"][0])
        duplicate["id"] = "duplicate"
        payload["profiles"].append(duplicate)
        with self.assertRaises(SettingsError):
            self.settings.save(payload)
        atomic_json(self.settings.profiles, {"version": 1, "profiles": [{"api_key": "NEVER_RETURN"}]})
        with self.assertRaises(SettingsError) as error:
            self.settings.view()
        self.assertNotIn("NEVER_RETURN", str(error.exception))

    def test_existing_ccswitch_route_and_new_model_id(self):
        payload = self.payload()
        payload["profiles"][0]["endpoint"]["native_auth"] = True
        payload["roles"]["worker"]["model"] = "another-vendor/new-model"
        payload["profiles"][0]["models"].append("another-vendor/new-model")
        self.settings.save(payload)
        config = read_config(self.config)
        self.assertTrue(config["worker"]["endpoint"]["native_auth"])
        self.assertEqual(config["worker"]["model"], "another-vendor/new-model")

    def test_http_save_requires_same_origin_and_does_not_call_provider(self):
        server = make_server(Store(self.root / "no-runs"), settings=self.settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            before = self.config.read_bytes()
            for headers in ({}, {"Origin": "http://unrelated.example", "X-Relay-Panel": "settings"}):
                request = urllib.request.Request(server.url + "api/settings", data=b"{}", headers={"Content-Type": "application/json", **headers})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            self.assertEqual(before, self.config.read_bytes())
            request = urllib.request.Request(server.url + "api/settings", data=b"{}", headers={"Content-Type": "application/json", "Origin": origin, "X-Relay-Panel": "settings", "Content-Length": "40000"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            payload = self.custom()
            request = urllib.request.Request(server.url + "api/settings", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Origin": origin, "X-Relay-Panel": "settings"})
            with urllib.request.urlopen(request, timeout=5) as response:
                result = json.load(response)
            self.assertEqual(result["roles"]["worker"]["model"], "vendor/code-model")
            # No key was installed and the example hostname is unreachable;
            # successful configuration saving never attempts provider access.
            self.assertEqual(read_config(self.config)["worker"]["model"], "vendor/code-model")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
