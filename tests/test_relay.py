import copy
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relaylib.contracts import RelayError, schemas, validate_plan, validate_result
from relaylib.gateway import Gateway, Ledger
from relaylib.runner import ROOT, Runner, read_config
from relaylib.runtime import FileLock, atomic_json, project_snapshot, run_process
from audit_ccswitch import audit

# Keep every fixture under the writable workspace, including restricted desktops.
TEST_TEMP = ROOT / ".test-tmp"
TEST_TEMP.mkdir(exist_ok=True)
tempfile.tempdir = str(TEST_TEMP)


class WorkspaceTemp:
    """Python 3.14's Windows mode=0700 temp ACL rejects restricted-token access.
    Use an ordinary inherited-ACL workspace directory instead.
    """
    def __init__(self, prefix="tmp"):
        self.path = TEST_TEMP / (prefix + uuid.uuid4().hex)
        self.path.mkdir()
        self.name = str(self.path)

    def __enter__(self):
        return self.name

    def __exit__(self, *_):
        self.cleanup()

    def cleanup(self):
        target = self.path.resolve()
        if not target.is_relative_to(TEST_TEMP.resolve()) or self.path.is_symlink():
            raise RuntimeError("Unsafe fixture cleanup target")
        if not self.path.exists():
            return

        def readonly(func, name, exc):
            checked = Path(name).resolve()
            if not checked.is_relative_to(target):
                raise exc
            os.chmod(name, 0o777)
            func(name)

        shutil.rmtree(self.path, onexc=readonly)


def plan():
    return {"goal": "Fix value", "codeFacts": [{"path": "value.txt", "symbol": "value", "lines": "1", "note": "Current value is wrong"}],
            "decisions": [{"id": "D1", "what": "Write fixed", "why": "Required output"}],
            "steps": [{"id": "S1", "do": "Fix value", "verify": "Run test"}],
            "expectedScope": ["value.txt"], "acceptance": ["test"], "mayAdjust": [], "escalateWhen": ["D1 cannot satisfy requirements"]}


def result(status="completed"):
    return {"status": status, "changed": [{"path": "value.txt", "summary": "Fixed"}], "checks": [],
            "residual": [], "failedStep": "", "decisionNeeded": "", "reason": "", "blockedReason": "", "attempted": [], "evidence": []}


class Upstream:
    def __init__(self, status=200):
        self.requests = []
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "body": json.loads(raw), "auth": self.headers.get("Authorization")})
                value = json.dumps({"ok": True}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp(prefix="stage-relay-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        (self.project / "value.txt").write_text("wrong")
        self.git("init", "-q")
        self.git("add", "value.txt")
        self.git("-c", "user.name=Relay test", "-c", "user.email=relay@example.invalid", "commit", "-qm", "baseline")
        self.upstream = Upstream()
        self.upstream.__enter__()
        self.addCleanup(self.upstream.__exit__, None, None, None)
        self.sequence = self.base / "sequence.json"
        self.patch_env = patch.dict(os.environ, {"RELAY_MOCK_SEQUENCE": str(self.sequence)})
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.config = json.loads((ROOT / "config.example.json").read_text())
        # Legacy flow cases isolate execution/check recovery. Review routing and
        # failures are covered separately in test_review with review enabled.
        self.config["review"] = False
        self.config["codex_command"] = [sys.executable, str(ROOT / "tests" / "fake_codex.py")]
        for role, model in (("planner", "planner-model"), ("worker", "worker-model")):
            self.config[role]["model"] = model
            self.config[role]["endpoint"] = {"base_url": self.upstream.url}
            self.config[role]["timeout_seconds"] = 10
        self.config["checks"] = {"test": [sys.executable, "-c", "from pathlib import Path; import sys; sys.exit(0 if Path('value.txt').read_text()=='fixed' else 1)"]}

    def git(self, *args):
        p = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def start(self, entries, runner_class=Runner):
        atomic_json(self.sequence, {"cursor": 0, "entries": entries})
        path = self.base / "config.json"
        atomic_json(path, self.config)
        runner = runner_class(read_config(path), self.project, self.base / "runs" / "case")
        state = runner.start("Fix the value to fixed")
        return runner, state

    def good_entries(self):
        return [{"response": plan()}, {"response": result(), "write": {"value.txt": "fixed"}}]

    def test_success_routes_stages_and_checks_independently(self):
        runner, state = self.start(self.good_entries())
        self.assertEqual(state["status"], "completed")
        self.assertEqual([r["body"]["model"] for r in self.upstream.requests], ["planner-model", "worker-model"])
        self.assertEqual(runner.ledger.value["roles"]["planner"]["forwarded"], 1)
        self.assertEqual(state["attempts"][0]["usage"]["usage"][0]["cached_input_tokens"], 80)
        self.assertIsNone(json.loads((runner.run_dir / "summary.json").read_text())["cost_usd"])
        self.assertTrue((runner.run_dir / "changes.patch").read_bytes())
        prompt = (runner.run_dir / "002-worker" / "input.txt").read_text()
        self.assertIn("Current value is wrong", prompt)
        self.assertNotIn("mock-thread", prompt)

    def test_independent_test_failure_returns_to_cheap_worker_only(self):
        runner, state = self.start([{"response": plan()}, {"response": result()},
                                    {"response": result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["fix_rounds"], 1)
        self.assertEqual([r["body"]["model"] for r in self.upstream.requests], ["planner-model", "worker-model", "worker-model"])

    def test_named_decision_can_trigger_one_replan(self):
        escalation = result("needs_replan")
        escalation.update(failedStep="S1", decisionNeeded="D1", reason="Original decision incompatible",
                          attempted=["Tried implementation"], evidence=["Relevant failure"])
        runner, state = self.start([{"response": plan()}, {"response": escalation},
                                    {"response": plan()}, {"response": result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["replans"], 1)

    def test_replan_cannot_repeat_forever(self):
        escalation = result("needs_replan")
        escalation.update(failedStep="S1", decisionNeeded="D1", reason="Need changed decision",
                          attempted=["Tried"], evidence=["Failure"])
        _, state = self.start([{"response": plan()}, {"response": escalation}, {"response": plan()}, {"response": escalation}])
        self.assertEqual(state["status"], "exhausted")
        self.assertEqual(len(self.upstream.requests), 4)

    def test_missing_escalation_evidence_never_calls_planner_again(self):
        _, state = self.start([{"response": plan()}, {"response": result("needs_replan")}])
        self.assertEqual(state["failure"]["code"], "protocol_error")
        self.assertEqual(len(self.upstream.requests), 2)

    def test_invalid_json_is_protocol_failure_not_replan(self):
        self.config.setdefault("limits", {})["max_fix_rounds"] = 0
        _, state = self.start([{"invalid_json": True}])
        self.assertEqual(state["status"], "awaiting_recovery")
        self.assertEqual(state["failure"]["code"], "protocol_error")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_api_failure_is_not_replan(self):
        self.upstream.status = 401
        _, state = self.start([{"response": plan()}])
        self.assertEqual(state["failure"]["code"], "authentication_error")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_prompt_contract_works_without_native_schema_support(self):
        runner, state = self.start(self.good_entries())
        self.assertEqual(state["status"], "completed")
        for role, schema in (("planner", "plan"), ("worker", "result")):
            body = next(r["body"] for r in self.upstream.requests if r["body"]["model"] == role + "-model")
            self.assertNotIn("text", body)
            self.assertIn(json.dumps(runner.definitions[schema], ensure_ascii=False, separators=(",", ":")), body["input"])

    def test_invalid_worker_json_repairs_report_without_repeating_edits(self):
        runner, state = self.start([{"response": plan()},
                                    {"invalid_json": True, "write": {"value.txt": "fixed"}},
                                    {"response": result()}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.upstream.requests), 3)
        repair = self.upstream.requests[-1]["body"]
        self.assertEqual(repair["model"], "worker-model")
        self.assertEqual(repair["fixture_sandbox"], "read-only")
        self.assertIn("FORMAT REPAIR ONLY", repair["input"])
        self.assertIn("NOT JSON", repair["input"])
        self.assertNotIn("USER REQUEST:", repair["input"])
        self.assertIn(str(runner.run_dir / "002-worker" / "input.txt"), repair["input"])
        original = (runner.run_dir / "002-worker" / "input.txt").read_text(encoding="utf-8")
        self.assertLess(len(repair["input"]), len(original))
        self.assertTrue(state["attempts"][1]["invalid_format"])
        self.assertEqual(runner.ledger.value["roles"]["worker"]["forwarded"], 2)

    def test_invalid_planner_shape_repairs_before_worker(self):
        _, state = self.start([{"response": {"goal": "missing fields"}}, {"response": plan()},
                               {"response": result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "completed")
        self.assertEqual([r["body"]["model"] for r in self.upstream.requests],
                         ["planner-model", "planner-model", "worker-model"])

    def test_format_repairs_stop_at_existing_configured_limit(self):
        self.config.setdefault("limits", {})["max_fix_rounds"] = 1
        _, state = self.start([{"invalid_json": True}, {"invalid_json": True}])
        self.assertEqual(state["status"], "awaiting_recovery")
        self.assertEqual(state["failure"]["code"], "protocol_error")
        self.assertEqual(len(self.upstream.requests), 2)

    def test_format_repair_cannot_modify_project(self):
        _, state = self.start([{"invalid_json": True}, {"response": plan(), "write": {"value.txt": "unauthorized"}}])
        self.assertEqual(state["failure"]["code"], "format_repair_modified_project")

    def test_wrong_model_is_blocked_before_upstream(self):
        _, state = self.start([{"response": plan(), "request_model": "unexpected-expensive-model"}])
        self.assertEqual(state["failure"]["code"], "model_mismatch")
        self.assertEqual(self.upstream.requests, [])

    def test_request_cap_stops_internal_cli_loop(self):
        self.config["planner"]["max_requests"] = 2
        runner, state = self.start([{"response": plan(), "requests": 20}])
        self.assertEqual(state["failure"]["code"], "request_limit")
        self.assertEqual(len(self.upstream.requests), 2)
        self.assertEqual(runner.ledger.value["roles"]["planner"]["forwarded"], 2)

    def test_timeout_kills_stage_without_escalation(self):
        self.config["planner"]["timeout_seconds"] = 0.1
        _, state = self.start([{"sleep": 20, "response": plan()}])
        self.assertEqual(state["failure"]["code"], "timeout")
        self.assertEqual(state["failure"]["timeout_seconds"], 0.1)
        self.assertGreaterEqual(state["failure"]["idle_seconds"], 0.1)
        self.assertIn("连续未收到输出或接口数据", state["failure"]["message"])
        self.assertEqual(self.upstream.requests, [])

    def test_project_dirty_is_rejected_before_model_work(self):
        (self.project / "value.txt").write_text("user edit")
        with self.assertRaises(RelayError) as caught:
            self.start(self.good_entries())
        self.assertEqual(caught.exception.code, "dirty_project")
        self.assertEqual(self.upstream.requests, [])

    def test_catalog_mismatch_fails_before_expensive_planning(self):
        atomic_json(self.sequence, {"cursor": 0, "entries": self.good_entries()})
        path = self.base / "config.json"
        atomic_json(path, self.config)
        runner = Runner(read_config(path), self.project, self.base / "runs" / "case")
        endpoint = {"url": self.upstream.url, "headers": {}, "catalog_models": ["planner-model"]}
        with patch("relaylib.runner.resolve_endpoint", return_value=endpoint):
            with self.assertRaises(RelayError) as caught:
                runner.start("Fix value")
        self.assertEqual(caught.exception.code, "model_catalog_mismatch")
        self.assertEqual(self.upstream.requests, [])

    def test_project_changed_after_checkpoint_is_not_silently_resumed(self):
        class CrashAfterPlan(Runner):
            def _accept_stage(self, role, directory, meta):
                raise KeyboardInterrupt()
        atomic_json(self.sequence, {"cursor": 0, "entries": self.good_entries()})
        path = self.base / "config.json"
        atomic_json(path, self.config)
        broken = CrashAfterPlan(read_config(path), self.project, self.base / "runs" / "case")
        with self.assertRaises(KeyboardInterrupt):
            broken.start("Fix value")
        (self.project / "value.txt").write_text("external change")
        with self.assertRaises(RelayError) as caught:
            Runner(broken.config, self.project, broken.run_dir).resume()
        self.assertEqual(caught.exception.code, "project_changed")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_planner_cannot_silently_modify_project(self):
        _, state = self.start([{"response": plan(), "write": {"value.txt": "bad planner edit"}}])
        self.assertEqual(state["failure"]["code"], "planner_modified_project")

    def test_model_cannot_inject_acceptance_command(self):
        bad = plan()
        bad["acceptance"] = ["DeleteEverything"]
        _, state = self.start([{"response": bad}])
        self.assertEqual(state["failure"]["code"], "protocol_error")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_missing_checks_does_not_claim_verified_success(self):
        self.config["checks"] = {}
        draft = plan()
        draft["acceptance"] = []
        _, state = self.start([{"response": draft}, {"response": result(), "write": {"value.txt": "fixed"}}])
        self.assertEqual(state["status"], "needs_manual_verification")

    def test_completed_stage_recovered_without_duplicate_paid_call(self):
        class CrashAfterPlan(Runner):
            def _accept_stage(self, role, directory, meta):
                raise KeyboardInterrupt()
        atomic_json(self.sequence, {"cursor": 0, "entries": self.good_entries()})
        path = self.base / "config.json"
        atomic_json(path, self.config)
        broken = CrashAfterPlan(read_config(path), self.project, self.base / "runs" / "case")
        with self.assertRaises(KeyboardInterrupt):
            broken.start("Fix value")
        resumed = Runner(broken.config, self.project, broken.run_dir)
        state = resumed.resume()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(self.upstream.requests), 2)

    def test_interrupted_unfinished_stage_requires_explicit_retry(self):
        self.config.setdefault("limits", {})["max_fix_rounds"] = 0
        runner, _ = self.start([{"invalid_json": True}])
        # No valid finished result can be automatically replayed.
        (runner.run_dir / "001-planner" / "finished.json").unlink()
        resumed = Runner(runner.config, self.project, runner.run_dir)
        with self.assertRaises(RelayError) as caught:
            resumed.resume()
        self.assertEqual(caught.exception.code, "interrupted")
        self.assertEqual(len(self.upstream.requests), 1)

    def test_blocked_requires_reason_and_attempts(self):
        blocked = result("blocked")
        blocked.update(blockedReason="Missing project fixture", attempted=["Checked tracked files"])
        _, state = self.start([{"response": plan()}, {"response": blocked}])
        self.assertEqual(state["status"], "blocked")


class GuardTests(unittest.TestCase):
    def test_native_credentials_are_only_forwarded_with_private_path(self):
        with WorkspaceTemp() as temp, Upstream() as upstream:
            ledger = Ledger(Path(temp) / "budget.json")
            with Gateway({"url": upstream.url, "headers": {}, "native_auth": True},
                         "m", "planner", 2, "native", ledger, Path(temp) / "audit.jsonl") as gate:
                req = urllib.request.Request(gate.base_url + "/responses", data=b'{"model":"m","input":"x"}',
                    headers={"Authorization": "Bearer native-fixture-secret", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    response.read()
                wrong = urllib.request.Request(f"http://127.0.0.1:{gate.server.server_port}/v1/responses", data=b'{"model":"m"}',
                    headers={"Authorization": "Bearer native-fixture-secret"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(wrong, timeout=5)
                self.assertEqual(caught.exception.code, 401)
                caught.exception.close()
            self.assertEqual(len(upstream.requests), 1)
            self.assertEqual(upstream.requests[0]["auth"], "Bearer native-fixture-secret")
            self.assertNotIn("native-fixture-secret", (Path(temp) / "audit.jsonl").read_text())

    def test_ledger_write_failure_denies_before_upstream(self):
        with WorkspaceTemp() as temp, Upstream() as upstream:
            ledger = Ledger(Path(temp) / "budget.json")
            with Gateway({"url": upstream.url, "headers": {}}, "m", "planner", 2, "io", ledger,
                         Path(temp) / "audit.jsonl") as gate, patch.object(ledger, "reserve", side_effect=OSError("disk full")):
                req = urllib.request.Request(gate.base_url + "/responses", data=b'{"model":"m"}',
                    headers={"Authorization": "Bearer " + gate.token})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(caught.exception.code, 503)
                caught.exception.close()
            self.assertEqual(upstream.requests, [])

    def test_request_count_is_atomic_under_concurrency(self):
        with WorkspaceTemp() as temp, Upstream() as upstream:
            ledger = Ledger(Path(temp) / "budget.json")
            with Gateway({"url": upstream.url, "headers": {"Authorization": "Bearer upstream-secret"}},
                         "m", "planner", 2, "one", ledger, Path(temp) / "audit.jsonl") as gate:
                def send(_):
                    req = urllib.request.Request(gate.base_url + "/responses",
                        data=b'{"model":"m","input":"test"}',
                        headers={"Authorization": "Bearer " + gate.token, "Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(req, timeout=5) as response:
                            response.read()
                    except urllib.error.HTTPError as error:
                        error.close()
                    except (urllib.error.URLError, OSError):
                        pass
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(send, range(8)))
            self.assertLessEqual(len(upstream.requests), 2)
            self.assertEqual(ledger.value["roles"]["planner"]["forwarded"], 2)
            self.assertTrue(all(r["auth"] == "Bearer upstream-secret" for r in upstream.requests))
            self.assertNotIn("upstream-secret", (Path(temp) / "audit.jsonl").read_text())
            restored = Ledger(Path(temp) / "budget.json")
            self.assertFalse(restored.reserve("planner", "m", 2, "resumed"))

    def test_lock_excludes_concurrent_runner(self):
        with WorkspaceTemp() as temp:
            path = Path(temp) / "lock"
            with FileLock(path):
                with self.assertRaises(RelayError):
                    with FileLock(path):
                        pass
            with FileLock(path):
                pass

    def test_usd_hard_cap_is_not_faked(self):
        with WorkspaceTemp() as temp:
            config = json.loads((ROOT / "config.example.json").read_text())
            config["budget"]["hard_cap_usd"] = 1
            path = Path(temp) / "config.json"
            atomic_json(path, config)
            with self.assertRaises(RelayError) as caught:
                read_config(path)
            self.assertEqual(caught.exception.code, "unsupported_hard_budget")

    def test_no_automatic_keep_warm_cost_assumption(self):
        with WorkspaceTemp() as temp:
            config = json.loads((ROOT / "config.example.json").read_text())
            config["planner"]["persist"] = "auto"
            path = Path(temp) / "config.json"
            atomic_json(path, config)
            with self.assertRaises(RelayError):
                read_config(path)

    def test_process_output_is_bounded(self):
        with WorkspaceTemp() as temp:
            p = Path(temp)
            outcome = run_process([sys.executable, "-c", "print('x'*1000000)"], temp, "", 5, 2000,
                                  p / "out", p / "err")
            self.assertEqual(outcome["failure"], "output_limit")
            self.assertLessEqual((p / "out").stat().st_size + (p / "err").stat().st_size, 2000)

    def test_stage_progress_can_outlast_timeout_interval(self):
        with WorkspaceTemp() as temp:
            p = Path(temp)
            script = "import time; [(print('working', flush=True), time.sleep(0.12)) for _ in range(9)]"
            outcome = run_process([sys.executable, "-u", "-c", script], temp, "", 0.4, 100000,
                                  p / "out", p / "err", progress_at=lambda: 0.0)
            self.assertIsNone(outcome["failure"])
            self.assertGreater(outcome["elapsed_seconds"], 0.4)

    def test_upstream_stream_progress_can_outlast_timeout_interval(self):
        with WorkspaceTemp() as temp:
            p = Path(temp)
            latest = [time.monotonic()]
            stopped = threading.Event()

            def heartbeat():
                while not stopped.wait(0.1):
                    latest[0] = time.monotonic()

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            try:
                outcome = run_process([sys.executable, "-c", "import time; time.sleep(1)"],
                                      temp, "", 0.4, 100000, p / "out", p / "err",
                                      progress_at=lambda: latest[0])
            finally:
                stopped.set()
                thread.join(timeout=2)
            self.assertIsNone(outcome["failure"])
            self.assertGreater(outcome["elapsed_seconds"], 0.4)

    def test_timeout_stops_descendant_heartbeat(self):
        with WorkspaceTemp() as temp:
            p = Path(temp)
            beat = p / "heartbeat"
            script = "import subprocess,sys,time; subprocess.Popen([sys.executable,sys.argv[1],'heartbeat',sys.argv[2]]); time.sleep(20)"
            outcome = run_process([sys.executable, "-c", script, str(ROOT / "tests" / "fake_codex.py"), str(beat)],
                                  temp, "", 0.8, 100000, p / "out", p / "err")
            self.assertEqual(outcome["failure"], "timeout")
            self.assertTrue(beat.exists())
            content = beat.read_bytes()
            time.sleep(0.3)
            self.assertEqual(beat.read_bytes(), content)


class AuditTests(unittest.TestCase):
    def fixture(self, parent, rows):
        run = parent / "run"
        stage = run / "001-planner"
        stage.mkdir(parents=True)
        (stage / "events.jsonl").write_text('{"type":"thread.started","thread_id":"owned"}\n', encoding="utf-8")
        database = parent / "ledger.db"
        con = sqlite3.connect(database)
        con.execute("CREATE TABLE proxy_request_logs (request_id TEXT,model TEXT,request_model TEXT,pricing_model TEXT,status_code INTEGER,total_cost_usd TEXT,input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,session_id TEXT,created_at INTEGER)")
        con.executemany("INSERT INTO proxy_request_logs VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        return audit(run, database)

    def test_no_matching_sessions_are_unknown_not_zero_cost(self):
        with WorkspaceTemp() as temp:
            value = self.fixture(Path(temp), [("1","m","m","m",200,"1",1,1,0,"unrelated",1)])
            self.assertEqual(value["matched_rows"], 0)
            self.assertIsNone(value["known_ledger_cost_usd"])
            self.assertIsNone(value["total_cost_usd"])

    def test_zero_pricing_is_unknown_and_not_added_as_free(self):
        with WorkspaceTemp() as temp:
            value = self.fixture(Path(temp), [("1","m","m","m",200,"0.02",1,1,0,"owned",1),
                                               ("2","review","review",None,200,"0",1,1,0,"owned",2)])
            self.assertEqual(value["known_ledger_cost_usd"], "0.02")
            self.assertEqual(value["unpriced_rows"], 1)
            self.assertIsNone(value["total_cost_usd"])

    def test_unrelated_account_usage_is_excluded(self):
        with WorkspaceTemp() as temp:
            value = self.fixture(Path(temp), [("1","m","m","m",200,"0.02",1,1,0,"owned",1),
                                               ("2","m","m","m",200,"99",1,1,0,"unrelated",2)])
            self.assertEqual(value["matched_rows"], 1)
            self.assertEqual(value["known_ledger_cost_usd"], "0.02")


if __name__ == "__main__":
    unittest.main()
