"""Installed Codex against scripted localhost Responses; never calls a paid model."""
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relaylib.runner import ROOT, Runner, read_config
from relaylib.runtime import atomic_json


def sse(items, model, identity):
    response = {"id": "resp_" + identity, "object": "response", "created_at": int(time.time()),
                "model": model, "status": "completed", "output": items,
                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                          "input_tokens_details": {"cached_tokens": 20},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index, "item": item})
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    events.append({"type": "response.completed", "response": response})
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()


def message(text):
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def main():
    parent = ROOT / "runs" / ("subagent-cli-" + uuid.uuid4().hex[:10])
    project = parent / "project"
    project.mkdir(parents=True)
    (project / "value.txt").write_text("fixture")
    from relaylib.runtime import git
    git(project, "init", "-q")
    git(project, "add", "value.txt")
    git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture")
    outcome = {"status": "completed", "changed": [], "checks": [], "residual": [], "answer": "OK",
               "failedStep": "", "decisionNeeded": "", "reason": "", "blockedReason": "", "attempted": [], "evidence": []}
    seen = []
    root_calls = [0]
    child_ids = []
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    child_prompts = [f"Read value.txt and reply CHILD_{i}_OK. Do not edit files or spawn agents." for i in range(2)]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            from relaylib.subagents import public_agent_id
            child_index = next((i for i, text in enumerate(child_prompts) if any(x.get("role") == "user" and any(c.get("text", "") == text for c in x.get("content", [])) for x in body.get("input", []))), None)
            with guard:
                seen.append({"model": body["model"], "thread_id": public_agent_id(self.headers), "child": child_index})
                atomic_json(parent / "requests.json", seen)
                response_id = str(len(seen))
            if child_index is not None:
                barrier.wait(timeout=20)  # Both children must really overlap.
                items = [message(f"CHILD_{child_index}_OK")]
            else:
                root_calls[0] += 1
                if root_calls[0] == 1:
                    items = [{"type": "function_call", "namespace": "multi_agent_v1", "name": "spawn_agent",
                              "call_id": f"call_spawn_{i}", "arguments": json.dumps({"message": text})} for i, text in enumerate(child_prompts)]
                else:
                    outputs = [x for x in body.get("input", []) if x.get("type") == "function_call_output"]
                    for output in outputs:
                        try:
                            child = json.loads(output.get("output", "{}"))
                            if child.get("agent_id") and child["agent_id"] not in child_ids:
                                child_ids.append(child["agent_id"])
                        except ValueError:
                            pass
                    if root_calls[0] == 2 and child_ids:
                        items = [{"type": "function_call", "namespace": "multi_agent_v1", "name": "wait_agent",
                                  "call_id": "call_wait", "arguments": json.dumps({"targets": child_ids})}]
                    elif root_calls[0] == 3 and child_ids:
                        items = [{"type": "function_call", "namespace": "multi_agent_v1", "name": "close_agent",
                                  "call_id": f"call_close_{i}", "arguments": json.dumps({"target": identity})} for i, identity in enumerate(child_ids)]
                    else:
                        items = [message(json.dumps(outcome))]
            data = sse(items, body["model"], response_id)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = json.loads((ROOT / "config.example.json").read_text())
    command = read_config(ROOT / "config.local.json")["codex_command"]
    config["codex_command"] = command
    config["subagents"] = {"mode": "auto", "max_concurrent": 2}
    config["review"] = False
    config["checks"] = {}
    for role in ("planner", "worker"):
        config[role].update(model="relay-fixture", endpoint={"base_url": f"http://127.0.0.1:{server.server_port}/v1"}, timeout_seconds=90)
    atomic_json(parent / "config.json", config)
    plan = {"goal": "Local protocol fixture", "codeFacts": [{"path": "value.txt", "symbol": "fixture", "note": "fixture", "lines": "1"}],
            "decisions": [{"id": "D1", "what": "No edits", "why": "Fixture"}],
            "steps": [{"id": "S1", "do": "Reply OK", "verify": "Read reply"}],
            "expectedScope": ["value.txt"], "acceptance": [], "mayAdjust": [], "escalateWhen": []}
    try:
        state = Runner(config, project, parent / "relay").start_with_plan(plan)
        print(json.dumps({"status": state["status"], "failure": state["failure"], "path": str(parent),
                          "requests": len(seen), "mode": "LOCAL_FAKE_NO_PAID_MODELS"}))
        from relaylib.subagents import lifecycle
        agents = lifecycle(parent / "relay" / "001-worker" / "events.jsonl")["agents"]
        assert len(agents) == 2 and all(a.get("closed") for a in agents), agents
        assert {a["answer"] for a in agents} == {"CHILD_0_OK", "CHILD_1_OK"}, agents
        assert len(seen) == 6 and all(r["model"] == "relay-fixture" for r in seen), len(seen)
        from relaylib.subagents import routed_usage
        usage = routed_usage(parent / "relay" / "requests.jsonl")
        assert usage["input_tokens"] == 600 and usage["output_tokens"] == 60, usage
        assert len(usage["agents"]) == 3, usage
        atomic_json(parent / "acceptance.json", {"agents": agents, "usage": usage, "parallel_barrier_passed": not barrier.broken, "paid_calls": 0})
        return 0 if not state.get("failure") else 1
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
