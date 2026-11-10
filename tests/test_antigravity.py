"""The Antigravity adapter must keep the shapes that were proven on the real app.

Each test here encodes something that was measured, not guessed: the workspace
URI scheme, the nesting of the model field, and the step mapping that carries a
real tool call. If one of these changes, the adapter silently stops working on
the installed app, so the shape itself is the assertion.
"""

import json
import tempfile
import unittest
from pathlib import Path

from relay.antigravity_worker import run_job
from relay.runtime import Runtime
from relay.domain import Store, Invalid
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

        run_job(Server(), Runtime(), "antigravity", "cascade-existing",
                {"id": "d1", "prompt": "do it"}, "MODEL_X", set())
        self.assertEqual(sent["cascade"], "cascade-existing")
        self.assertEqual(sent["text"], "do it")

    def test_a_delivery_without_a_prompt_is_refused(self):
        class Runtime:
            def receipt(self, *a, **k):
                raise AssertionError("must not report delivery for an empty prompt")

        with self.assertRaises(Invalid):
            run_job(object(), Runtime(), "antigravity", "c", {"id": "d"}, "M", set())

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
                return {"jobs": [{"id": "d1", "session": "c1", "prompt": "go"}]}

            def observe(self, *a, **k):
                pass

            def lease(self, *a, **k):
                return {"renewed": True}

            def receipt(self, *a, **k):
                pass

        class Panel:
            def __init__(self):
                self.runtime = Runtime()

        class App:
            def steps(self, cascade):
                return []

            def models(self):
                return [{"model": "M"}]

            def send(self, *a):
                delivered.append(a)

        loop = AntigravityLoop(Panel(), port=0)
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