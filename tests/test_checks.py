"""Checks: catalog/generation/compat with scripted local gateways; never a paid model."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import test_relay as fixtures
from relaylib import checks
from relaylib.plugin import JobStore
from relaylib.settings import Settings
from relaylib.runtime import atomic_json
from panel import Store, make_server


def gateway_body(text="OK"):
    """A minimal Responses SSE stream carrying assistant text."""
    payload = json.dumps({"type": "response.output_text.delta", "delta": text})
    return ("event: response.output_text.delta\n" + "data: " + payload + "\n\n" +
            'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n').encode()


class ScriptedGateway:
    """Local /v1/models + /v1/responses; counts every request and can fail."""

    def __init__(self, catalog_status=200, models=("check-model",), answer="OK"):
        self.requests = []
        self.catalog_status = catalog_status
        self.models = list(models)
        self.answer = answer
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body, mime="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                body = json.dumps({"data": [{"id": model} for model in outer.models]}).encode()
                self.reply(outer.catalog_status, body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append(("POST", self.path, json.loads(raw)))
                self.reply(200, gateway_body(), "text/event-stream")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.config = self.f.base / "config.json"
        atomic_json(self.config, self.f.config)
        self.root = self.f.base / "checks-root"
        self.root.mkdir()
        self.settings = Settings(self.config)

    def gateway_config(self, url):
        config = json.loads(json.dumps(self.f.config))
        for role in ("planner", "worker"):
            config[role]["endpoint"] = {"base_url": url, "api_key_env": "RELAY_CHECK_TEST_KEY"}
        atomic_json(self.config, config)
        atomic_json(self.config.with_name("providers.local.json"), {"version": 1, "profiles": [
            {"id": "check-provider", "name": "Fixture gateway",
             "endpoint": {"base_url": url, "api_key_env": "RELAY_CHECK_TEST_KEY"},
             "models": ["check-model"]}]})
        return Settings(self.config)

    def script_real_cli(self, answer):
        """A tiny CLI speaking the same argv/stdin contract as Codex."""
        script = self.f.base / "scripted_cli.py"
        template = (
            "import json, os, sys, urllib.request\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "def opt(name): return args[args.index(name) + 1]\n"
            "config = {}\n"
            "for i, item in enumerate(args):\n"
            "    if item == '-c':\n"
            "        key, value = args[i + 1].split('=', 1)\n"
            "        try: config[key] = json.loads(value)\n"
            "        except ValueError: config[key] = value\n"
            "prompt = sys.stdin.buffer.read()\n"
            "body = json.dumps({'model': opt('-m'), 'input': prompt.decode('utf-8')}).encode()\n"
            "base = config['model_providers.stage_relay.base_url']\n"
            "request = urllib.request.Request(base + '/responses', data=body,\n"
            "    headers={'Authorization': 'Bearer ' + os.environ['STAGE_RELAY_TOKEN'],\n"
            "             'Content-Type': 'application/json'})\n"
            "with urllib.request.urlopen(request, timeout=10) as response: response.read()\n"
            "Path(opt('-o')).write_text(__ANSWER__, encoding='utf-8')\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n"
        )
        script.write_text(template.replace("__ANSWER__", repr(answer)), encoding="utf-8")
        settings = self.gateway_config(self.f.upstream.url)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["codex_command"] = [sys.executable, str(script)]
        atomic_json(self.config, config)
        return Settings(self.config)

    def test_catalog_check_reports_status_and_never_generates(self):
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                record = checks.run_check(self.root, settings, "check-provider", "check-model", "catalog")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["model_calls"], 0)
        self.assertEqual([row[0] for row in gateway.requests], ["GET"])
        self.assertIn("目录", record["message"])

    def test_missing_model_and_http_401_fail_with_status_and_no_generation(self):
        with ScriptedGateway(models=("other-model",)) as gateway:
            settings = self.gateway_config(gateway.url)
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                record = checks.run_check(self.root, settings, "check-provider", "check-model", "catalog")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["code"], "model_catalog_mismatch")
        self.assertEqual([row[0] for row in gateway.requests], ["GET"])
        self.assertEqual(record["model_calls"], 0)
        with ScriptedGateway(catalog_status=401) as gateway:
            settings = self.gateway_config(gateway.url)
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                record = checks.run_check(self.root, settings, "check-provider", "check-model", "catalog")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["http_status"], 401)
        self.assertEqual(record["model_calls"], 0)

    def test_generation_and_compat_use_one_request_and_a_temporary_sample(self):
        # The fixture CLI posts to the same loopback gateway the check starts,
        # so record POSTs via the check's own temporary requests log.
        result = fixtures.result()
        for kind, answer, expected in (("generation", "OK", "passed"),
                                       ("compat", json.dumps(result), "passed"),
                                       ("compat", "NOT JSON", "failed")):
            with self.subTest(kind=kind, answer=answer[:10]):
                with ScriptedGateway() as gateway:
                    settings = self.script_real_cli(answer)
                    before = sorted(p.name for p in self.f.project.iterdir())
                    with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                        record = checks.run_check(self.root, settings, "check-provider", "check-model", kind)
                self.assertEqual(record["status"], expected, record)
                self.assertEqual(record["model_calls"], 1)
                self.assertEqual(record["http_status"], 200)
                self.assertEqual(sorted(p.name for p in self.f.project.iterdir()), before)
                self.assertFalse(list(self.f.base.glob("stage-relay-check-*")))

    def test_generation_failure_reports_stage_and_real_http_status(self):
        # Upstream rejects the generation request: both fields must be present.
        class Rejecting(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                body = json.dumps({"data": [{"id": "check-model"}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = b'{"error":{"message":"SECRET"}}'
                self.send_response(403); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Rejecting)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.f.upstream.status = 200
        url = f"http://127.0.0.1:{server.server_port}/v1"
        settings = self.gateway_config(url)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["codex_command"] = [sys.executable, str(self.f.base / "scripted_cli.py")]
        atomic_json(self.config, config)
        cli = self.f.base / "scripted_cli.py"
        cli.write_text(
            "import json, os, sys, urllib.request\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "opt = lambda n: args[args.index(n) + 1]\n"
            "config = {}\n"
            "for i, a in enumerate(args):\n"
            "    if a == '-c':\n"
            "        k, v = args[i + 1].split('=', 1)\n"
            "        try: config[k] = json.loads(v)\n"
            "        except ValueError: config[k] = v\n"
            "prompt = sys.stdin.buffer.read()\n"
            "body = json.dumps({'model': opt('-m'), 'input': prompt.decode()}).encode()\n"
            "req = urllib.request.Request(config['model_providers.stage_relay.base_url'] + '/responses', data=body,\n"
            "    headers={'Authorization': 'Bearer ' + os.environ['STAGE_RELAY_TOKEN']})\n"
            "try: urllib.request.urlopen(req, timeout=10).read()\n"
            "except urllib.error.HTTPError as exc: sys.exit(3)\n"
            "Path(opt('-o')).write_text('SHOULD NOT RUN', encoding='utf-8')\n"
            "print(json.dumps({'type': 'turn.completed'}))\n", encoding="utf-8")
        settings = Settings(self.config)
        with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
            record = checks.run_check(self.root, settings, "check-provider", "check-model", "generation")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["http_status"], 403)
        # The stage names the failed step, not just "model check".
        self.assertEqual(record["stage"], "上游模型请求")
        self.assertIn("认证", record["message"])
        self.assertNotIn("SECRET", json.dumps(record, ensure_ascii=False))
        self.assertEqual(record["model_calls"], 1)

    def test_http_200_process_failure_keeps_redacted_diagnostics(self):
        settings = self.script_real_cli("OK")
        script = self.f.base / "scripted_cli.py"
        script.write_text(script.read_text(encoding="utf-8") +
                          "\nprint(os.environ['STAGE_RELAY_TOKEN'], os.environ['RELAY_CHECK_TEST_KEY'], file=sys.stderr)\n"
                          "raise SystemExit(1)\n", encoding="utf-8")
        with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "sensitive-fixture-key"}):
            record = checks.run_check(self.root, settings, "check-provider", "check-model", "generation")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["stage"], "运行模型进程")
        self.assertEqual(record["model_calls"], 1)
        self.assertTrue(record["logs"])
        content = "\n".join(fixtures.Path(path).read_text(encoding="utf-8") for path in record["logs"])
        self.assertNotIn("sensitive-fixture-key", content)
        self.assertIn("[REDACTED]", content)

    def test_turn_failed_cannot_pass_with_zero_exit_code(self):
        settings = self.script_real_cli("OK")
        script = self.f.base / "scripted_cli.py"
        script.write_text(script.read_text(encoding="utf-8") +
                          "\nprint(json.dumps({'type':'turn.failed'}))\n", encoding="utf-8")
        with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
            record = checks.run_check(self.root, settings, "check-provider", "check-model", "generation")
        self.assertEqual(record["code"], "process_or_api_error")
        self.assertEqual(record["stage"], "运行模型进程")

    def test_local_failure_does_not_claim_a_model_call(self):
        settings = self.gateway_config(self.f.upstream.url)
        with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}), \
             patch.object(checks, "run_process", side_effect=checks.RelayError("permission_denied", "")):
            record = checks.run_check(self.root, settings, "check-provider", "check-model", "generation")
        self.assertEqual(record["model_calls"], 0)
        self.assertFalse(self.f.upstream.requests)

    def test_checks_never_create_a_job(self):
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                checks.run_check(self.root, settings, "check-provider", "check-model", "catalog")
        self.assertEqual(list((self.root).glob("[a-f0-9]" * 24)), [])
        self.assertFalse((self.root / "jobs").exists())

    def test_compat_fails_when_local_schema_rejects_the_answer(self):
        with ScriptedGateway() as gateway:
            settings = self.script_real_cli(json.dumps({"status": "completed"}))
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                record = checks.run_check(self.root, settings, "check-provider", "check-model", "compat")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["code"], "protocol_error")
        self.assertEqual(record["stage"], "本地 JSON 校验")
        self.assertEqual(record["http_status"], 200)

    def test_records_invalidate_after_endpoint_or_credential_change(self):
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                passed = checks.run_check(self.root, settings, "check-provider", "check-model", "catalog")
                self.assertTrue(checks.list_records(self.root, settings)["records"][0]["valid"])
                changed = json.loads(self.config.read_text(encoding="utf-8"))
                changed["worker"]["endpoint"] = {"base_url": gateway.url, "api_key_env": "OTHER_KEY"}
                atomic_json(self.config, changed)
                rows = checks.list_records(self.root, Settings(self.config))["records"]
        self.assertEqual(passed["status"], "passed")
        self.assertFalse(rows[0]["valid"])
        self.assertTrue(rows[0]["invalid_reason"])

    def test_unknown_model_and_kind_are_rejected_before_any_request(self):
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            with self.assertRaises(Exception):
                checks.run_check(self.root, settings, "check-provider", "absent", "catalog")
            with self.assertRaises(Exception):
                checks.run_check(self.root, settings, "check-provider", "check-model", "everything")
        self.assertEqual(gateway.requests, [])

    def test_http_route_is_explicit_and_same_origin(self):
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            jobs = JobStore(self.config, self.root / "jobs", [self.f.project])
            server = make_server(Store(jobs.root), settings=settings, jobs=jobs)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            body = json.dumps({"provider_id": "check-provider", "model": "check-model", "kind": "catalog"}).encode()
            with patch.dict(os.environ, {"RELAY_CHECK_TEST_KEY": "fixture"}):
                with urllib.request.urlopen(server.url + "api/checks") as response:
                    listing = json.load(response)
                self.assertEqual(listing["records"], [])
                self.assertEqual(gateway.requests, [])
                request = urllib.request.Request(server.url + "api/check", data=body, headers={
                    "Content-Type": "application/json", "X-Relay-Panel": "settings",
                    "Origin": "http://127.0.0.1:" + str(server.server_port)})
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
        self.assertEqual(result["status"], "passed")
        self.assertEqual([row[0] for row in gateway.requests], ["GET"])
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            jobs = JobStore(self.config, self.root / "jobs2", [self.f.project])
            server = make_server(Store(jobs.root), settings=settings, jobs=jobs)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            bad = urllib.request.Request(server.url + "api/check", data=body, headers={
                "Content-Type": "application/json", "X-Relay-Panel": "settings",
                "Origin": "http://untrusted.invalid"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(bad)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        self.assertEqual(gateway.requests, [])

    def test_reading_panel_routes_never_starts_a_check(self):
        with ScriptedGateway() as gateway:
            settings = self.gateway_config(gateway.url)
            jobs = JobStore(self.config, self.root / "jobs3", [self.f.project])
            server = make_server(Store(jobs.root), settings=settings, jobs=jobs)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            for route in ("api/settings", "api/launch", "api/draft", "api/runs", "api/checks"):
                with urllib.request.urlopen(server.url + route) as response:
                    response.read()
        self.assertEqual(gateway.requests, [])


if __name__ == "__main__":
    unittest.main()
