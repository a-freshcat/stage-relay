"""The Antigravity adapter must keep the shapes that were proven on the real app.

Each test here encodes something that was measured, not guessed: the workspace
URI scheme, the nesting of the model field, and the step mapping that carries a
real tool call. If one of these changes, the adapter silently stops working on
the installed app, so the shape itself is the assertion.
"""

import io
import json
import tempfile
import os
import unittest
from unittest.mock import patch
from pathlib import Path

from relay.antigravity_loop import AntigravityLoop
from relay.antigravity_worker import observe_steps, run_job, settled
from relay.runtime import Runtime
from relay.domain import Store, Invalid
from relay.antigravity import OutcomeUnknown, NotSent
from relay.antigravity import (
    AntigravityServer,
    activity_of,
    finished,
    workspace_uri,
)
from relay.domain import Invalid

TOOL = "CORTEX_STEP_TYPE_CODE_ACTION"
ASSISTANT = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
USER = "CORTEX_STEP_TYPE_USER_INPUT"
ERROR = "CORTEX_STEP_TYPE_ERROR_MESSAGE"


class WorkspaceUri(unittest.TestCase):
    def test_a_windows_path_becomes_a_file_uri(self):
        """The server rejects bare `C:/...` with `unknown uri scheme`."""
        uri = workspace_uri(Path("C:/Users/example/space"))
        self.assertTrue(uri.startswith("file:///"), uri)
        self.assertNotIn("\\", uri)

    def test_a_relative_path_is_resolved_first(self):
        uri = workspace_uri(".")
        self.assertTrue(uri.startswith("file:///"), uri)


class ModelConfig(unittest.TestCase):
    def test_the_model_sits_under_planner_config(self):
        """One level up is silently ignored by the app, so the nesting is the contract."""
        self.assertEqual(AntigravityServer.config("MODEL_X"), {"plannerConfig": {"planModel": "MODEL_X"}})

    def test_without_a_model_the_request_is_refused_before_sending(self):
        with self.assertRaises(Invalid):
            AntigravityServer.config("")
        with self.assertRaises(Invalid):
            AntigravityServer.config(None)


class ModelPolicy(unittest.TestCase):
    """A restricted model must block dispatch instead of silently spending quota.

    The panel cannot pick a cascade's model, so the only honest enforcement is to
    read the model the app recorded and refuse when it is not allowed.
    """

    def loop(self):
        class Panel:
            pass
        return AntigravityLoop(Panel(), 0)

    def test_without_a_policy_nothing_is_refused(self):
        loop = self.loop()
        self.assertEqual(loop.allowed_models(), set())
        self.assertIsNone(loop.model_refusal("c1"))

    def test_a_permitted_model_passes(self):
        loop = self.loop()
        loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M318"
        with patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV: "MODEL_PLACEHOLDER_M318"}):
            self.assertIsNone(loop.model_refusal("c1"))

    def test_a_forbidden_model_is_refused_and_named(self):
        loop = self.loop()
        loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M418"
        with patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV: "MODEL_PLACEHOLDER_M318"}):
            refusal = loop.model_refusal("c1")
        self.assertIn("MODEL_PLACEHOLDER_M418", refusal)
        self.assertIn("MODEL_PLACEHOLDER_M318", refusal)

    def test_an_unrecorded_model_is_refused_rather_than_assumed(self):
        """Not knowing the model is not the same as knowing it is allowed."""
        loop = self.loop()
        loop.session_model = lambda cascade_id: None
        with patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV: "MODEL_PLACEHOLDER_M318"}):
            refusal = loop.model_refusal("c1")
        self.assertIn("无法确认", refusal)

    def test_a_recorded_restriction_survives_the_process(self):
        """The restriction is standing, so it cannot live only in an env var."""
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            policy.record(folder, {"MODEL_PLACEHOLDER_M318": "Gemini 3.8 Flash (High)"})
            self.assertEqual(policy.allowed_ids(folder), {"MODEL_PLACEHOLDER_M318"})

    def test_an_unreadable_record_refuses_instead_of_allowing(self):
        """A damaged policy file must fail closed, not open."""
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / policy.FILENAME).write_text("{not json", encoding="utf-8")
            self.assertIsNone(policy.allowed_ids(folder))
            loop = self.loop()
            loop.panel.store = type("S", (), {"path": Path(folder) / "db.sqlite3"})()
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M318"
            refusal = loop.model_refusal("c1")
        self.assertIn("无法读取", refusal)

    def test_a_recorded_policy_is_read_from_the_data_directory(self):
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            policy.record(folder, {"MODEL_PLACEHOLDER_M318": "Gemini 3.8 Flash (High)"})
            loop = self.loop()
            loop.panel.store = type("S", (), {"path": Path(folder) / "db.sqlite3"})()
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M418"
            refusal = loop.model_refusal("c1")
            self.assertIn("MODEL_PLACEHOLDER_M418", refusal)
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M318"
            self.assertIsNone(loop.model_refusal("c1"))

    def test_an_environment_variable_cannot_widen_a_recorded_restriction(self):
        """A rule the user asked for is not something env gets to override.

        The union of the record and the variable let a per-run setting admit a
        model the record had excluded — the restriction was silently gone.
        """
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            policy.record(folder, {"MODEL_PLACEHOLDER_M318": "Flash"},
                          projects=["qa"])
            loop = self.loop()
            loop.panel.store = type("S", (), {"path": Path(folder) / "db.sqlite3"})()
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M418"
            with patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV:
                                         "MODEL_PLACEHOLDER_M418"}):
                refusal = loop.model_refusal("c1", {"id": "qa"})
        self.assertIsNotNone(refusal, "环境变量不得放宽已记录的限制")

    def test_an_environment_variable_may_only_narrow(self):
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            policy.record(folder, {"MODEL_PLACEHOLDER_M318": "Flash",
                                   "MODEL_PLACEHOLDER_M319": "Flash"},
                          projects=["qa"])
            loop = self.loop()
            loop.panel.store = type("S", (), {"path": Path(folder) / "db.sqlite3"})()
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M319"
            with patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV:
                                         "MODEL_PLACEHOLDER_M318"}):
                refusal = loop.model_refusal("c1", {"id": "qa"})
        self.assertIsNotNone(refusal, "收窄后 M319 应被排除")

    def test_a_recorded_but_empty_allow_list_refuses(self):
        """An empty allow-list forbids everything; it is not 'no policy'."""
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / policy.FILENAME).write_text(
                json.dumps({"allowed": []}), encoding="utf-8")
            self.assertIsNone(policy.allowed_ids(folder))
            loop = self.loop()
            loop.panel.store = type("S", (), {"path": Path(folder) / "db.sqlite3"})()
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M418"
            refusal = loop.model_refusal("c1")
        self.assertIn("无法读取", refusal)

    def test_a_record_missing_its_allow_list_refuses(self):
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / policy.FILENAME).write_text(
                json.dumps({"projects": ["qa"]}), encoding="utf-8")
            self.assertIsNone(policy.allowed_ids(folder))

    def test_no_record_at_all_is_the_only_unrestricted_case(self):
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(policy.allowed_ids(folder), set())
            loop = self.loop()
            loop.panel.store = type("S", (), {"path": Path(folder) / "db.sqlite3"})()
            loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M418"
            self.assertIsNone(loop.model_refusal("c1"))

    def test_a_restriction_does_not_follow_a_user_into_other_projects(self):
        """A QA-only model rule must not bind an ordinary session."""
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            policy.record(folder, {"MODEL_PLACEHOLDER_M318": "Flash"},
                          projects=["qa-project"])
            self.assertEqual(policy.allowed_ids(folder, {"id": "qa-project"}),
                             {"MODEL_PLACEHOLDER_M318"})
            self.assertEqual(policy.allowed_ids(folder, {"id": "real-project"}), set())

    def test_an_unscoped_record_governs_every_project(self):
        from relay import policy
        with tempfile.TemporaryDirectory() as folder:
            policy.record(folder, {"MODEL_PLACEHOLDER_M318": "Flash"})
            self.assertEqual(policy.allowed_ids(folder, {"id": "any"}),
                             {"MODEL_PLACEHOLDER_M318"})
            self.assertEqual(policy.allowed_ids(folder), {"MODEL_PLACEHOLDER_M318"})

    def test_the_policy_is_a_set_not_a_prefix_match(self):
        loop = self.loop()
        loop.session_model = lambda cascade_id: "MODEL_PLACEHOLDER_M3189"
        with patch.dict(os.environ, {AntigravityLoop.MODEL_POLICY_ENV: "MODEL_PLACEHOLDER_M318"}):
            self.assertIsNotNone(loop.model_refusal("c1"))

class ActivityMapping(unittest.TestCase):
    def test_a_real_tool_call_is_reported_with_its_arguments(self):
        steps = [{
            "type": TOOL,
            "status": "CORTEX_STEP_STATUS_DONE",
            "metadata": {
                "source": "CORTEX_STEP_SOURCE_MODEL",
                "toolCall": {
                    "id": "call_1",
                    "name": "write_file",
                    "argumentsJson": '{"Path":"a.txt","Content":"x"}',
                },
            },
        }]
        events = activity_of(steps)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool/call")
        self.assertEqual(events[0]["data"]["name"], "write_file")
        self.assertEqual(events[0]["data"]["call_id"], "call_1")
        self.assertEqual(events[0]["data"]["source"], "CORTEX_STEP_SOURCE_MODEL")

    def test_an_error_step_carries_the_model_message(self):
        steps = [{
            "type": ERROR,
            "errorMessage": {"error": {
                "modelErrorMessage": "plan model not specified",
                "userErrorMessage": "Agent execution terminated due to error.",
            }},
        }]
        event = activity_of(steps)[0]
        self.assertEqual(event["type"], "error")
        self.assertIn("plan model", event["data"]["message"])

    def test_only_new_steps_are_reported(self):
        """The trajectory is append-only, so index is the cursor."""
        steps = [
            {"type": USER},
            {"type": ASSISTANT, "status": "CORTEX_STEP_STATUS_DONE"},
            {"type": TOOL, "metadata": {"toolCall": {"name": "write_file"}}},
        ]
        first = activity_of(steps)
        self.assertEqual(len(first), 3)
        again = activity_of(steps, seen={0, 1})
        self.assertEqual([e["index"] for e in again], [2])

    def test_a_failed_mcp_call_is_reported_as_failed_with_its_reason(self):
        """An MCP failure lives in `error.shortError`, not `errorMessage`.

        Reading only `errorMessage` made these look like successful calls, which
        is how a denied call could be reported as working.
        """
        steps = [{
            "type": "CORTEX_STEP_TYPE_MCP_TOOL",
            "status": "CORTEX_STEP_STATUS_ERROR",
            "error": {"shortError": "你不属于此任务，无法读取。"},
            "mcpTool": {
                "serverName": "stage-relay-antigravity",
                "resultString": "你不属于此任务，无法读取。",
                "toolCall": {
                    "name": "relay_task",
                    "argumentsJson": json.dumps({"ToolName": "relay_task",
                                                 "Arguments": {"project_id": "p"}}),
                },
            },
        }]
        event = activity_of(steps)[0]
        self.assertEqual(event["type"], "mcp/call")
        self.assertEqual(event["data"]["tool"], "relay_task")
        self.assertEqual(event["data"]["status"], "CORTEX_STEP_STATUS_ERROR")
        self.assertEqual(event["data"]["error"], "你不属于此任务，无法读取。")

    def test_a_successful_mcp_call_has_no_error(self):
        steps = [{
            "type": "CORTEX_STEP_TYPE_MCP_TOOL",
            "status": "CORTEX_STEP_STATUS_DONE",
            "mcpTool": {
                "serverName": "stage-relay-antigravity",
                "toolCall": {"name": "relay_inbox", "argumentsJson": json.dumps({"ToolName": "relay_inbox"})},
            },
        }]
        event = activity_of(steps)[0]
        self.assertEqual(event["type"], "mcp/call")
        self.assertIsNone(event["data"]["error"])

    def test_appending_to_an_existing_cascade_is_what_the_worker_sends(self):
        """The worker must reuse a cascade, never start one.

        A fresh cascade rejects its first message on this install, so a worker
        that started one would report success for a turn that never ran.
        """
        sent = {}

        class Server:
            def send(self, cascade, text, model):
                sent.update(cascade=cascade, text=text, model=model)

            def steps(self, cascade):
                return [{"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                         "status": "CORTEX_STEP_STATUS_DONE"}]

        class Runtime:
            def receipt(self, *a, **k):
                pass

            def lease(self, *a, **k):
                pass

            def observe(self, *a, **k):
                pass

        outcome = run_job(Server(), Runtime(), "antigravity", "cascade-existing",
                          {"id": "d1", "prompt": "do it"}, "MODEL_X", {})
        self.assertEqual(outcome["outcome"], "settled")
        self.assertEqual(sent["cascade"], "cascade-existing")
        self.assertEqual(sent["text"], "do it")

    def test_a_delivery_without_a_prompt_is_marked_failed_never_delivered(self):
        """Nothing was sent, so the record must not say it was handed over."""
        seen = []

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

        with self.assertRaises(Invalid):
            run_job(object(), Runtime(), "antigravity", "c", {"id": "d"}, "M", {})
        self.assertEqual(seen, ["failed"])

    def test_a_new_antigravity_session_is_refused_rather_than_faked(self):
        """Creation is an open defect; the panel must not claim it works."""
        runtime = Runtime(Store(Path(tempfile.mkdtemp()) / "db.sqlite3"))
        project = runtime.store.apply("project_create", {
            "title": "t", "goal": "g", "workspace": str(Path(tempfile.mkdtemp()))})
        with self.assertRaises(Invalid) as caught:
            runtime.create_session(project["id"], "antigravity")
        self.assertIn("不支持新建会话", str(caught.exception))

    def test_the_loop_takes_each_lease_once_and_delivers_it(self):
        """A lease taken by advertise() must be delivered, never dropped.

        The earlier draft polled once per cascade: the first poll leased the job,
        the cascade lookup then failed, and the job was stranded in `leased`
        where no later tick could reclaim it.
        """
        from relay.antigravity_loop import AntigravityLoop

        delivered = []

        class Runtime:
            def __init__(self):
                self.calls = 0

            def poll(self, agent, payload):
                self.calls += 1
                return {"jobs": [{"id": "d1", "session": "c1", "prompt": "go",
                                  "project": "p1", "generation": 1}]}

            def observe(self, *a, **k):
                pass

            def lease(self, *a, **k):
                return {"renewed": True}

            def receipt(self, *a, **k):
                pass

            def snapshot(self):
                return {"bindings": [{"project": "p1", "agent": "antigravity",
                                      "session": "c1", "generation": 1}]}

        class App:
            def __init__(self):
                self.sent = 0

            def steps(self, cascade):
                # One step before sending, plus this turn's own reply: without the
                # growth the baseline correctly says "the old turn is still there".
                steps = [{"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                          "status": "CORTEX_STEP_STATUS_DONE"}]
                if self.sent:
                    steps.append({"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                                  "status": "CORTEX_STEP_STATUS_DONE"})
                return steps

            def models(self):
                return [{"model": "M"}]

            def send(self, *a):
                self.sent += 1
                delivered.append(a)

        def make_panel(runtime, project=None):
            """A panel stub with what the loop's guard and advertise touch."""
            class Store:
                @staticmethod
                def snapshot():
                    return {"projects": [project or {
                        "id": "p1", "demo": False, "paused": False, "closed": False,
                        "workspace": "W"}]}

            class Panel:
                store = Store()

                def __init__(self):
                    self.runtime = runtime

            return Panel()

        loop = AntigravityLoop(make_panel(Runtime()), port=0)
        loop.session_model = lambda cascade_id: "M"
        import relay.antigravity_loop as module
        original = module.cascades
        module.cascades = lambda app: [{"id": "c1", "status": "CASCADE_RUN_STATUS_IDLE",
                                        "workspace": "W"}]
        original_connect = loop.connect
        loop.connect = lambda: App()
        try:
            loop.tick()
        finally:
            module.cascades = original
            loop.connect = original_connect
        self.assertEqual(loop.panel.runtime.calls, 1, "exactly one poll per tick")
        # Delivery now runs on its own thread so the heartbeat keeps beating.
        for thread in list(loop._threads.values()):
            thread.join(10)
        self.assertEqual(loop.snapshot()["delivered"], 1)
        self.assertEqual(len(delivered), 1)

    def test_the_loop_reports_offline_instead_of_inventing_a_delivery(self):
        from relay.antigravity_loop import AntigravityLoop

        class Panel:
            class runtime:
                @staticmethod
                def poll(*a, **k):
                    raise AssertionError("must not lease while the app is offline")

        loop = AntigravityLoop(Panel(), port=0)
        loop.connect = lambda: (_ for _ in ()).throw(OSError("no app"))
        loop.tick()
        self.assertFalse(loop.snapshot()["online"])
        self.assertEqual(loop.snapshot()["delivered"], 0)

    def test_a_rejected_send_is_failed_and_never_recorded_as_delivered(self):
        """`delivered` must mean the app accepted it, not that we tried."""
        seen = []

        class Server:
            def send(self, *a):
                # What the real client raises when the socket dies mid-call.
                raise OutcomeUnknown("transport closed")

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

            def lease(self, *a, **k):
                raise AssertionError("must not start polling a turn that never began")

            def observe(self, *a, **k):
                pass

        with self.assertRaises(OutcomeUnknown):
            run_job(Server(), Runtime(), "antigravity", "c", {"id": "d1", "prompt": "go"}, "M", {})
        # A dropped connection cannot prove the message never arrived, so the
        # record must say uncertain rather than assert a failure.
        self.assertEqual(seen, ["uncertain"])
        self.assertNotIn("delivered", seen)

    def test_a_refused_send_is_failed_because_it_is_provably_not_sent(self):
        seen = []

        class Server:
            def send(self, *a):
                raise Invalid("模型未指定")

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

            def lease(self, *a, **k):
                raise AssertionError("must not poll")

            def observe(self, *a, **k):
                pass

        with self.assertRaises(Invalid):
            run_job(Server(), Runtime(), "antigravity", "c", {"id": "d1", "prompt": "go"}, "M", {})
        self.assertEqual(seen, ["failed"])

    def test_a_stale_binding_is_refused_before_anything_is_sent(self):
        """A rebind or pause while waiting must not deliver into the old session."""
        seen, sent = [], []

        class Server:
            def send(self, *a):
                sent.append(a)

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

        def guard(job):
            return "会话已换绑，原租约不再有效"

        with self.assertRaises(Invalid):
            run_job(Server(), Runtime(), "antigravity", "c",
                    {"id": "d1", "prompt": "go"}, "M", {}, guard=guard)
        self.assertEqual(sent, [])
        self.assertEqual(seen, ["failed"])

    def test_a_step_that_finishes_is_reported_again_with_its_completion(self):
        """RUNNING then DONE is one step becoming finished, not a duplicate."""
        reported = {}

        class Runtime:
            def __init__(self):
                self.events = []

            def observe(self, agent, payload):
                self.events.append(payload["event"]["data"])

        runtime = Runtime()
        running = [{"type": "CORTEX_STEP_TYPE_MCP_TOOL", "status": "CORTEX_STEP_STATUS_RUNNING",
                    "mcpTool": {"toolCall": {"name": "relay_inbox", "argumentsJson": "{}"}}}]
        observe_steps(runtime, "antigravity", "c", running, reported)
        finished = [{"type": "CORTEX_STEP_TYPE_MCP_TOOL", "status": "CORTEX_STEP_STATUS_DONE",
                     "mcpTool": {"toolCall": {"name": "relay_inbox", "argumentsJson": "{}"}}}]
        observe_steps(runtime, "antigravity", "c", finished, reported)
        self.assertEqual(len(runtime.events), 2, "completion must be sent")
        self.assertEqual(runtime.events[0]["status"], "CORTEX_STEP_STATUS_RUNNING")
        self.assertEqual(runtime.events[1]["status"], "CORTEX_STEP_STATUS_DONE")
        # Re-sending the same state is still suppressed.
        observe_steps(runtime, "antigravity", "c", finished, reported)
        self.assertEqual(len(runtime.events), 2)

    def test_a_turn_whose_tail_is_running_is_not_settled(self):
        steps = [{"type": "CORTEX_STEP_TYPE_MCP_TOOL", "status": "CORTEX_STEP_STATUS_RUNNING"}]
        self.assertFalse(settled(steps))
        steps = [{"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "status": "CORTEX_STEP_STATUS_DONE"}]
        self.assertTrue(settled(steps))

    def test_a_timeout_is_reported_as_timeout_not_as_completion(self):
        seen = []

        class Server:
            def send(self, *a):
                pass

            def steps(self, cascade):
                return [{"type": "CORTEX_STEP_TYPE_MCP_TOOL", "status": "CORTEX_STEP_STATUS_RUNNING"}]

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

            def lease(self, *a, **k):
                return {"renewed": True}

            def observe(self, *a, **k):
                pass

        import relay.antigravity_worker as module
        original_poll, original_max = module.POLL_SECONDS, module.MAX_TURN_SECONDS
        module.POLL_SECONDS, module.MAX_TURN_SECONDS = 0.0, 0.05
        try:
            outcome = run_job(Server(), Runtime(), "antigravity", "c", {"id": "d", "prompt": "go"}, "M", {})
        finally:
            module.POLL_SECONDS, module.MAX_TURN_SECONDS = original_poll, original_max
        self.assertEqual(outcome["outcome"], "timeout")
        self.assertFalse(outcome["settled"])
        self.assertIn("delivered", seen)   # the app did accept the message
        self.assertEqual(seen.count("delivered"), 1)

    def test_the_loop_keeps_a_timed_out_session_busy(self):
        """A live turn must not receive a second delivery."""
        from relay.antigravity_loop import AntigravityLoop

        class Runtime:
            def __init__(self):
                self.polls = 0

            def poll(self, agent, payload):
                self.polls += 1
                return {"jobs": [{"id": "d1", "session": "c1", "prompt": "go",
                                  "project": "p1", "generation": 1}]}

            def receipt(self, *a, **k):
                pass

            def lease(self, *a, **k):
                return {"renewed": True}

            def observe(self, *a, **k):
                pass

            def snapshot(self):
                return {"bindings": [{"project": "p1", "agent": "antigravity",
                                      "session": "c1", "generation": 1}]}

        class App:
            def steps(self, cascade):
                return [{"type": "CORTEX_STEP_TYPE_MCP_TOOL", "status": "CORTEX_STEP_STATUS_RUNNING"}]

            def models(self):
                return [{"model": "M"}]

            def send(self, *a):
                pass

        def make_panel(runtime, project=None):
            """A panel stub with what the loop's guard and advertise touch."""
            class Store:
                @staticmethod
                def snapshot():
                    return {"projects": [project or {
                        "id": "p1", "demo": False, "paused": False, "closed": False,
                        "workspace": "W"}]}

            class Panel:
                store = Store()

                def __init__(self):
                    self.runtime = runtime

            return Panel()

        loop = AntigravityLoop(make_panel(Runtime()), port=0)
        import relay.antigravity_loop as module
        import relay.antigravity_worker as worker
        original = module.cascades
        module.cascades = lambda app: [{"id": "c1", "status": "CASCADE_RUN_STATUS_IDLE",
                                        "workspace": "W"}]
        loop.connect = lambda: App()
        original_poll, original_max = worker.POLL_SECONDS, worker.MAX_TURN_SECONDS
        worker.POLL_SECONDS, worker.MAX_TURN_SECONDS = 0.0, 0.05
        try:
            loop.tick()
            for thread in list(loop._threads.values()):
                thread.join(10)
            before = loop.snapshot()["delivered"]
            # A second tick must not start another delivery for the same session.
            loop.tick()
            for thread in list(loop._threads.values()):
                thread.join(10)
        finally:
            module.cascades = original
            worker.POLL_SECONDS, worker.MAX_TURN_SECONDS = original_poll, original_max
        self.assertEqual(loop.snapshot()["delivered"], before)
        self.assertEqual(loop.snapshot()["delivered"], 0)
        self.assertIn("c1", loop._busy)

    def test_a_busy_session_is_advertised_as_running(self):
        """The Runtime must not lease a second job for a session we are running.

        Trusting the app's own idle status would let it lease again while this
        worker is mid-turn, and the first delivery would be orphaned.
        """
        from relay.antigravity_loop import AntigravityLoop

        seen = {}

        class Runtime:
            def poll(self, agent, payload):
                seen["sessions"] = payload["sessions"]
                return {"jobs": []}

        class Store:
            @staticmethod
            def snapshot():
                return {"projects": []}

        class Panel:
            store = Store()

            def __init__(self):
                self.runtime = Runtime()

        loop = AntigravityLoop(Panel(), port=0)
        loop._busy.add("c1")
        hosted = [{"id": "c1", "status": "CASCADE_RUN_STATUS_IDLE", "workspace": "W"},
                  {"id": "c2", "status": "CASCADE_RUN_STATUS_IDLE", "workspace": "W"}]
        loop.advertise(hosted)
        status = {s["id"]: s["status"] for s in seen["sessions"]}
        self.assertEqual(status["c1"], "running", "busy session must read running")
        self.assertEqual(status["c2"], "idle")

    def test_an_empty_catalogue_is_still_published(self):
        """Silence would look like a dead host; the empty list is the signal."""
        from relay.antigravity_loop import AntigravityLoop

        seen = {}

        class Runtime:
            def poll(self, agent, payload):
                seen["called"] = True
                seen["sessions"] = payload["sessions"]
                return {"jobs": []}

        class Store:
            @staticmethod
            def snapshot():
                return {"projects": []}

        class Panel:
            store = Store()

            def __init__(self):
                self.runtime = Runtime()

        loop = AntigravityLoop(Panel(), port=0)
        loop.advertise([])
        self.assertTrue(seen["called"])
        self.assertEqual(seen["sessions"], [])

    def test_a_long_turn_is_observed_until_the_app_settles_it(self):
        """A timeout must keep watching, not claim the turn ended."""
        from relay.antigravity_loop import AntigravityLoop

        class App:
            def __init__(self):
                self.calls = 0

            def steps(self, cascade):
                self.calls += 1
                # Still running on the first look, finished on the second.
                if self.calls < 2:
                    return [{"type": "CORTEX_STEP_TYPE_MCP_TOOL",
                             "status": "CORTEX_STEP_STATUS_RUNNING"}]
                return [{"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                         "status": "CORTEX_STEP_STATUS_DONE"}]

        class Runtime:
            def lease(self, *a, **k):
                return {"renewed": True}

            def observe(self, *a, **k):
                pass

        class Panel:
            def __init__(self):
                self.runtime = Runtime()

        loop = AntigravityLoop(Panel(), port=0)
        settled = loop.observe_until_settled(App(), "c1", {"id": "d1"}, {})
        self.assertTrue(settled)

    def test_observation_stops_when_the_app_goes_away(self):
        from relay.antigravity_loop import AntigravityLoop

        class App:
            def steps(self, cascade):
                raise OSError("app closed")

        class Runtime:
            def lease(self, *a, **k):
                return {"renewed": True}

        class Panel:
            def __init__(self):
                self.runtime = Runtime()

        loop = AntigravityLoop(Panel(), port=0)
        self.assertFalse(loop.observe_until_settled(App(), "c1", {"id": "d1"}, {}))

    def test_a_real_timeout_through_the_client_is_recorded_uncertain(self):
        """The classification must survive the real client, not just a fake.

        `AntigravityServer.call` used to wrap every network error in `Invalid`, so
        the worker could not tell a refusal from a lost reply and reported a
        timeout as "failed". This drives the real client: only `urlopen` is
        patched, everything below it is the shipping code.
        """
        seen = []

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

            def lease(self, *a, **k):
                raise AssertionError("a lost send must not start polling")

            def observe(self, *a, **k):
                pass

        server = AntigravityServer(1234, "token", secure=False)
        original = AntigravityServer.call.__globals__["urllib"].request.urlopen
        import urllib.error as ue

        def timing_out(*a, **k):
            raise ue.URLError(TimeoutError("timed out"))

        AntigravityServer.call.__globals__["urllib"].request.urlopen = timing_out
        try:
            with self.assertRaises(OutcomeUnknown):
                run_job(server, Runtime(), "antigravity", "c",
                        {"id": "d1", "prompt": "go"}, "M", {})
        finally:
            AntigravityServer.call.__globals__["urllib"].request.urlopen = original
        self.assertEqual(seen, ["uncertain"])

    def test_a_real_http_error_through_the_client_is_recorded_failed(self):
        """A status code proves the server read the request and refused it."""
        seen = []

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

            def lease(self, *a, **k):
                raise AssertionError("must not poll")

            def observe(self, *a, **k):
                pass

        server = AntigravityServer(1234, "token", secure=False)
        import urllib.error as ue
        original = AntigravityServer.call.__globals__["urllib"].request.urlopen

        def refusing(request, *a, **k):
            raise ue.HTTPError(request.full_url, 400, "Bad Request", {},
                               io.BytesIO(b'{"error":"contents is not specified"}'))

        AntigravityServer.call.__globals__["urllib"].request.urlopen = refusing
        try:
            with self.assertRaises(NotSent):
                run_job(server, Runtime(), "antigravity", "c",
                        {"id": "d1", "prompt": "go"}, "M", {})
        finally:
            AntigravityServer.call.__globals__["urllib"].request.urlopen = original
        self.assertEqual(seen, ["failed"])

    def test_an_old_tail_is_not_taken_as_this_turns_result(self):
        """Before any new step, the tail still belongs to the previous turn."""
        seen = []

        class Server:
            def send(self, *a):
                pass

            def steps(self, cascade):
                return [{"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                         "status": "CORTEX_STEP_STATUS_DONE"}]

        class Runtime:
            def receipt(self, agent, payload):
                seen.append(payload["status"])

            def lease(self, *a, **k):
                return {"renewed": True}

            def observe(self, *a, **k):
                pass

        import relay.antigravity_worker as module
        original_poll, original_max = module.POLL_SECONDS, module.MAX_TURN_SECONDS
        module.POLL_SECONDS, module.MAX_TURN_SECONDS = 0.0, 0.03
        try:
            outcome = run_job(Server(), Runtime(), "antigravity", "c",
                              {"id": "d", "prompt": "go"}, "M", {}, steps_before=1)
        finally:
            module.POLL_SECONDS, module.MAX_TURN_SECONDS = original_poll, original_max
        self.assertEqual(outcome["outcome"], "timeout")
        self.assertFalse(outcome["settled"])
        self.assertEqual(outcome["steps"], 1)

    def test_an_observation_error_after_acceptance_keeps_the_session_busy(self):
        """A read failure during observation must not look like a failed send.

        The worker returns only after polling, so a caller that learns of
        acceptance on return never sees it when the poll raises. This drives the
        real `run_job` and the real `_deliver` together: acceptance must reach
        `_deliver` at the send boundary, and the session must stay busy.
        """
        from relay.antigravity_loop import AntigravityLoop

        receipts = []

        class App:
            def __init__(self):
                self.sent = 0
                self.reads = 0

            def steps(self, cascade):
                self.reads += 1
                if self.sent == 0:
                    return []              # the pre-send baseline is empty
                # After sending, the first observation raises the same error a
                # refused observation HTTP call raises.
                raise NotSent("观测被拒")

            def send(self, *a):
                self.sent += 1

            def models(self):
                return [{"model": "M"}]

        class Runtime:
            def poll(self, agent, payload):
                return {"jobs": [{"id": "d1", "session": "c1", "prompt": "go",
                                  "project": "p1", "generation": 1}]}

            def receipt(self, agent, payload):
                receipts.append(payload["status"])

            def lease(self, *a, **k):
                return {"renewed": True}

            def observe(self, *a, **k):
                pass

            def snapshot(self):
                return {"bindings": [{"project": "p1", "agent": "antigravity",
                                      "session": "c1", "generation": 1}]}

        class Store:
            @staticmethod
            def snapshot():
                return {"projects": [{"id": "p1", "demo": False, "paused": False,
                                      "closed": False, "workspace": "W"}]}

        class Panel:
            store = Store()

            def __init__(self):
                self.runtime = Runtime()

        app = App()

        class Wiring(App):
            def __init__(self, inner):
                self.inner = inner

        loop = AntigravityLoop(Panel(), port=0)
        loop.session_model = lambda cascade_id: "M"
        import relay.antigravity_loop as module
        import relay.antigravity_worker as worker
        original = module.cascades
        module.cascades = lambda a: [{"id": "c1", "status": "CASCADE_RUN_STATUS_IDLE",
                                      "workspace": "W"}]
        loop.connect = lambda: app
        original_sleep, original_max = worker.POLL_SECONDS, worker.MAX_TURN_SECONDS
        worker.POLL_SECONDS, worker.MAX_TURN_SECONDS = 0.0, 0.5
        try:
            loop.tick()
            for thread in list(loop._threads.values()):
                thread.join(10)
        finally:
            module.cascades = original
            worker.POLL_SECONDS, worker.MAX_TURN_SECONDS = original_sleep, original_max
        self.assertEqual(receipts, ["delivered"])
        # Accepted, then observation failed: still in flight, never released.
        self.assertIn("c1", loop._busy, "an accepted turn must stay busy")
        self.assertEqual(loop.snapshot()["delivered"], 0)
        self.assertEqual(loop.snapshot().get("unknown"), 1)

    def test_a_provably_unsent_job_does_release_the_session(self):
        """The mirror case: a refusal leaves nothing running."""
        from relay.antigravity_loop import AntigravityLoop

        receipts = []

        class App:
            def steps(self, cascade):
                return []

            def send(self, *a):
                raise NotSent("400 contents is not specified")

            def models(self):
                return [{"model": "M"}]

        class Runtime:
            def poll(self, agent, payload):
                return {"jobs": [{"id": "d1", "session": "c1", "prompt": "go",
                                  "project": "p1", "generation": 1}]}

            def receipt(self, agent, payload):
                receipts.append(payload["status"])

            def lease(self, *a, **k):
                return {"renewed": True}

            def observe(self, *a, **k):
                pass

            def snapshot(self):
                return {"bindings": [{"project": "p1", "agent": "antigravity",
                                      "session": "c1", "generation": 1}]}

        class Store:
            @staticmethod
            def snapshot():
                return {"projects": [{"id": "p1", "demo": False, "paused": False,
                                      "closed": False, "workspace": "W"}]}

        class Panel:
            store = Store()

            def __init__(self):
                self.runtime = Runtime()

        loop = AntigravityLoop(Panel(), port=0)
        loop.session_model = lambda cascade_id: "M"
        import relay.antigravity_loop as module
        original = module.cascades
        module.cascades = lambda a: [{"id": "c1", "status": "CASCADE_RUN_STATUS_IDLE",
                                      "workspace": "W"}]
        loop.connect = lambda: App()
        try:
            loop.tick()
            for thread in list(loop._threads.values()):
                thread.join(10)
        finally:
            module.cascades = original
        self.assertEqual(receipts, ["failed"])
        self.assertNotIn("c1", loop._busy, "nothing is running, so release it")

    def test_a_leased_job_carries_its_project_so_a_guard_can_check_it(self):
        """Without the project a guard cannot verify the lease, only guess.

        Reading `job["project"]` off the poll result used to yield None, which the
        guard reported as "no such project" and refused every real delivery.
        """
        runtime = Runtime(Store(Path(tempfile.mkdtemp()) / "db.sqlite3"))
        workspace = tempfile.mkdtemp()
        project = runtime.store.apply("project_create", {
            "title": "p", "goal": "g", "workspace": workspace})
        runtime.store.apply("agent_save", {"id": "antigravity", "app": "antigravity",
                                          "name": "Antigravity"})
        runtime.store.apply("assign", {"project_id": project["id"],
                                       "assignments": {"planner": "codex",
                                                       "executor": "antigravity",
                                                       "reporter": "codex"}})
        issue = runtime.store.apply("issue_create", {
            "project_id": project["id"], "title": "i", "body": "b",
            "members": ["antigravity"], "task_ids": []}, actor="codex")
        runtime.store.apply("message", {"project_id": project["id"],
                                        "issue_id": issue["id"], "body": "do it",
                                        "mentions": ["antigravity"]}, actor="codex")
        session = "cascade-x"
        runtime.poll("antigravity", {"app": "antigravity",
                                     "sessions": [{"id": session, "status": "idle",
                                                   "workspace": workspace}]})
        runtime.bind(project["id"], "antigravity", session)
        jobs = runtime.poll("antigravity", {"app": "antigravity",
                                            "sessions": [{"id": session,
                                                          "status": "idle",
                                                          "workspace": workspace}]})["jobs"]
        mine = [j for j in jobs if j["session"] == session]
        self.assertTrue(mine, "the mention must lease a job")
        self.assertEqual(mine[0]["project"], project["id"])
        self.assertIsNotNone(mine[0].get("generation"))

    def test_an_unknown_step_is_still_reported(self):
        event = activity_of([{"type": "CORTEX_STEP_TYPE_SOMETHING_NEW"}])[0]
        self.assertEqual(event["type"], "step")
        self.assertEqual(event["data"]["name"], "CORTEX_STEP_TYPE_SOMETHING_NEW")


class Completion(unittest.TestCase):
    def test_an_error_ends_the_turn(self):
        self.assertTrue(finished([{"type": USER}, {"type": ERROR}]))

    def test_an_empty_trajectory_is_not_finished(self):
        self.assertFalse(finished([]))

    def test_a_pending_user_turn_is_not_finished(self):
        self.assertFalse(finished([{"type": USER}]))

    def test_a_pending_tool_after_a_done_message_is_not_finished(self):
        """A newer unfinished tool outranks an older done message.

        Walking back to the first done assistant step would call this finished
        while the app is still writing the file.
        """
        steps = [
            {"type": USER},
            {"type": ASSISTANT, "status": "CORTEX_STEP_STATUS_DONE"},
            {"type": TOOL, "status": "CORTEX_STEP_STATUS_RUNNING",
             "metadata": {"toolCall": {"name": "write_file"}}},
        ]
        self.assertFalse(finished(steps))

    def test_a_stale_error_does_not_end_a_recovered_turn(self):
        """An error from an earlier attempt must not settle a newer turn."""
        steps = [
            {"type": ERROR},
            {"type": USER},
            {"type": ASSISTANT, "status": "CORTEX_STEP_STATUS_RUNNING"},
        ]
        self.assertFalse(finished(steps))

    def test_a_done_tool_followed_by_a_done_message_is_finished(self):
        steps = [
            {"type": USER},
            {"type": TOOL, "status": "CORTEX_STEP_STATUS_DONE"},
            {"type": ASSISTANT, "status": "CORTEX_STEP_STATUS_DONE"},
        ]
        self.assertTrue(finished(steps))


if __name__ == "__main__":
    unittest.main()