"""Delegation contracts and accounting use fixtures only; no paid model calls."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
from relaylib.contracts import RelayError
from relaylib.plugin import JobStore, execution_config
from relaylib.runner import Runner, read_config
from relaylib.runtime import atomic_json
from relaylib.subagents import (UsageCollector, cli_options, instructions, lifecycle,
                               policy, public_agent_id, routed_usage)


def event(tool, state="completed", status="completed", identity="c1", call=None):
    return {"type": "item.completed", "item": {"id": call or tool, "type": "collab_tool_call", "tool": tool,
        "status": status, "receiver_thread_ids": [identity], "prompt": "Read the assigned file",
        "agents_states": {identity: {"status": state, "message": "CHILD_OK" if state == "completed" else None}}}}


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = fixtures.WorkspaceTemp("delegation-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_policy_rejects_overlap_traversal_and_missing_tasks(self):
        task = {"goal": "Fix", "access": "write", "files": ["src/File.py"]}
        for value in ({"mode": "manual"}, {"mode": "auto", "tasks": [task]},
                      {"mode": "manual", "tasks": [task, {**task, "files": ["src/file.py"]}]},
                      {"mode": "manual", "tasks": [{**task, "files": ["../outside"]}]},
                      {"mode": "manual", "tasks": [{**task, "files": ["C:\\outside"]}]},
                      {"mode": "manual", "tasks": [task, {**task, "files": ["src"]}]}):
            with self.subTest(value=value), self.assertRaises(RelayError):
                policy(value, self.path)
        self.assertEqual(policy()["mode"], "off")
        self.assertEqual(policy({"mode": "manual", "tasks": [task, {**task, "access": "read"}]} )["mode"], "manual")

    def test_cli_inherits_unknown_model_and_preserves_native_concurrency(self):
        native = self.path / "native"
        native.mkdir()
        (native / "config.toml").write_text('[agents]\nmax_threads=9\ndefault_subagent_model="another-model"\n')
        with patch.dict("os.environ", {"CODEX_HOME": str(native)}):
            args = cli_options({"mode": "auto"}, "third-party-model")
            self.assertIn("agents={enabled=true,max_threads=9}", args)
            self.assertNotIn("default_subagent_model", " ".join(args))
            self.assertIn("agents={enabled=true,max_concurrent_threads_per_session=3}", cli_options({"mode": "auto", "max_concurrent": 3}, "third-party-model"))
        self.assertIn("features.multi_agent=false", cli_options({"mode": "auto"}, "ds", enabled=False))
        self.assertNotIn("max_output_tokens", " ".join(args))

    def test_prompt_authorizes_only_opted_in_execution(self):
        self.assertIn("Do not spawn", instructions(None))
        prompt = instructions({"mode": "auto"})
        for text in ("explicitly authorizes", "omit model", "critical-path", "close every child", "not separate"):
            self.assertIn(text, prompt)

    def test_full_lifecycle_retains_answer_and_requires_close(self):
        path = self.path / "events.jsonl"
        entries = [event("spawn_agent", "pending_init"), event("wait")]
        path.write_text("\n".join(map(json.dumps, entries)))
        self.assertEqual(lifecycle(path)["unfinished"], ["c1"])
        entries.append(event("close_agent"))
        path.write_text("\n".join(map(json.dumps, entries)))
        result = lifecycle(path, "ds")
        self.assertEqual(result["unfinished"], [])
        self.assertEqual(result["agents"][0]["answer"], "CHILD_OK")
        self.assertEqual(result["agents"][0]["model"], "ds")

    def test_incomplete_or_rejected_spawn_is_a_failure(self):
        path = self.path / "events.jsonl"
        path.write_text(json.dumps(event("spawn_agent", status="in_progress")))
        self.assertEqual(lifecycle(path)["failed_calls"], ["spawn_agent"])

    def test_resuming_a_closed_child_requires_closing_it_again(self):
        path = self.path / "events.jsonl"
        entries = [event("spawn_agent"), event("close_agent"), event("resume_agent"), event("wait")]
        path.write_text("\n".join(map(json.dumps, entries)))
        self.assertEqual(lifecycle(path)["unfinished"], ["c1"])
        entries.append(event("close_agent", call="close-again"))
        path.write_text("\n".join(map(json.dumps, entries)))
        self.assertEqual(lifecycle(path)["unfinished"], [])

    def test_usage_collector_handles_chunk_boundaries_without_double_count(self):
        raw = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":100,"output_tokens":20,"input_tokens_details":{"cached_tokens":40}}}}\r\n\r\n'
        collector = UsageCollector("text/event-stream")
        for offset in range(0, len(raw), 7):
            collector.feed(raw[offset:offset+7])
        self.assertEqual(collector.finish(), {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 40})
        # Telemetry overflow does not raise or limit the caller's response.
        collector = UsageCollector("application/json")
        collector.feed(b"x" * 4_000_001)
        self.assertIsNone(collector.finish())
        collector = UsageCollector("application/json")
        collector.feed(b'{"usage":{"input_tokens":1,"output_tokens":2}}')
        self.assertIsNone(collector.finish()["cached_input_tokens"])

    def test_unknown_usage_and_safe_thread_attribution(self):
        identity = "01a15764-ac86-7f13-895b-bef500a0fae5"
        self.assertEqual(public_agent_id({"x-codex-turn-metadata": json.dumps({"thread_id": identity, "secret": "NEVER_LOG"})}), identity)
        self.assertIsNone(public_agent_id({"session_id": "SECRET"}))
        log = self.path / "requests.jsonl"
        rows = [{"event": "request_forwarded", "request_id": "r1", "agent_id": identity},
                {"event": "request_forwarded", "request_id": "r2", "agent_id": identity},
                {"event": "response_usage", "request_id": "r1", "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}}]
        log.write_text("\n".join(map(json.dumps, rows)))
        usage = routed_usage(log)
        self.assertFalse(usage["complete"])
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(usage["agents"][identity]["requests"], 2)

    def test_actual_panel_renderer_shows_child_answer_and_unknown_usage(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
function node(tag,text,cls){return {tag,text:text||'',cls,children:[],append(...children){this.children.push(...children)},replaceChildren(){this.children=[]}}}
const elements={};const $=id=>elements[id]||=(node('div'));
const source=fs.readFileSync(process.argv[1],'utf8');
const render=source.slice(source.indexOf('function renderSubagents('),source.indexOf('function addDelegationTask('));
const ctx={node,$};vm.createContext(ctx);vm.runInContext(render,ctx);
ctx.renderSubagents({phases:[{id:'001-worker',role:'worker',subagents:{agents:[{id:'a',model:'ds',goal:'目标',status:'errored',answer:'具体失败',closed:true}]},routed_usage:{requests:2,complete:false,agents:{}}}]});
assert.equal($('subagents-results').hidden,false);
assert.ok(JSON.stringify($('subagents-cards')).includes('具体失败'));
assert.ok(JSON.stringify($('subagents-cards')).includes('has-problem'));
assert.ok($('subagents-usage').textContent.includes('合计未知'));
ctx.renderSubagents({phases:[]});assert.equal($('subagents-results').hidden,true);
'''
        from relaylib.runner import ROOT
        result = subprocess.run([shutil.which("node") or "node", "-e", script, str(ROOT / "panel/panel.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RelayIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.f.config.update(review=False, subagents={"mode": "auto"})

    def execute(self, events):
        atomic_json(self.f.sequence, {"cursor": 0, "entries": [{"response": fixtures.result(), "events": events, "write": {"value.txt": "fixed"}}]})
        runner = Runner(self.f.config, self.f.project, self.f.base / "runs" / "delegation")
        return runner, runner.start_with_plan(fixtures.plan())

    def test_unfinished_child_not_accepted_and_existing_changes_preserved(self):
        runner, state = self.execute([event("spawn_agent", "running")])
        self.assertEqual(state["failure"]["code"], "subagents_incomplete")
        self.assertFalse((runner.run_dir / "result.json").exists())
        self.assertEqual((self.f.project / "value.txt").read_text(), "fixed")

    def test_closed_child_accepted_and_exposed_to_observer(self):
        runner, state = self.execute([event("spawn_agent", "running"), event("wait"), event("close_agent")])
        self.assertEqual(state["status"], "completed")
        from panel import Store
        store = Store(runner.run_dir.parent)
        snapshot = store.snapshot(Store.identity(runner.run_dir))
        self.assertEqual(snapshot["phases"][0]["subagents"]["agents"][0]["answer"], "CHILD_OK")

    def test_manual_mode_requires_actual_delegation(self):
        self.f.config["subagents"] = {"mode": "manual", "tasks": [{"goal": "Read file", "access": "read", "files": ["value.txt"]}]}
        _, state = self.execute([])
        self.assertEqual(state["failure"]["code"], "subagents_incomplete")

    def test_submission_persists_policy_and_includes_it_in_idempotency(self):
        from test_plugin import handoff
        path = self.f.base / "config.json"
        atomic_json(path, self.f.config)
        store = JobStore(path, self.f.base / "jobs", [self.f.project])
        with patch.object(store, "launch"):
            result = store.submit(self.f.project, "same", handoff(), subagents={"mode": "off"})
            saved = read_config(store.directory(result["job_id"]) / "config.json")
            self.assertEqual(saved["subagents"], {"mode": "off"})
            with self.assertRaises(RelayError) as error:
                store.submit(self.f.project, "same", handoff(), subagents={"mode": "auto"})
            self.assertEqual(error.exception.code, "task_key_conflict")

    def test_http_submission_accepts_policy_and_rejects_bad_file_ownership_before_launch(self):
        import threading
        import urllib.request
        import urllib.error
        from panel import Store, make_server
        from relaylib.settings import Settings
        from test_plugin import handoff
        path = self.f.base / "config.json"
        atomic_json(path, self.f.config)
        store = JobStore(path, self.f.base / "httpjobs", [self.f.project])
        server = make_server(Store(store.root), settings=Settings(path), jobs=store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def post(value):
            from urllib.parse import urlsplit
            url = urlsplit(server.url)
            request = urllib.request.Request(server.url + "api/jobs", data=json.dumps(value).encode(), headers={"Content-Type": "application/json", "X-Relay-Panel": "settings", "Origin": url.scheme + "://" + url.netloc})
            return urllib.request.urlopen(request, timeout=5)
        body = {"project": str(self.f.project), "task_key": "http", "plan": handoff(), "subagents": {"mode": "auto"}}
        with patch.object(store, "launch") as launch:
            with post(body) as response:
                job = json.load(response)
            self.assertEqual(read_config(store.directory(job["job_id"]) / "config.json")["subagents"], {"mode": "auto"})
            body.update(task_key="bad", subagents={"mode": "manual", "tasks": [{"goal": "x", "access": "write", "files": ["../escape"]}]})
            with self.assertRaises(urllib.error.HTTPError) as error:
                post(body)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            self.assertEqual(launch.call_count, 1)
