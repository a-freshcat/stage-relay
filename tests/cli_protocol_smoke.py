"""Real installed Codex CLI against a localhost SSE fixture; NO paid models.

Not part of unittest discovery: run explicitly to check the local CLI build.
"""
import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relaylib.runner import ROOT, Runner, read_config
from relaylib.runtime import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", action="store_true", help="Exercise caller-plan jobs with the real CLI and a fake SSE endpoint")
    args = parser.parse_args()
    parent = ROOT / "runs" / (("plugin-cli-" if args.plugin else "cli-protocol-") + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    project = parent / "project"
    project.mkdir(parents=True)
    (project / "value.txt").write_text("fixture")
    for argv in (["init", "-q"], ["add", "value.txt"],
                 ["-c", "user.name=Protocol fixture", "-c", "user.email=relay@example.invalid", "commit", "-qm", "Fixture"]):
        subprocess.run(["git", "-C", str(project), *argv], capture_output=True, check=True)
    draft = {"goal": "Inspect fixture", "codeFacts": [{"path": "value.txt", "symbol": "value", "lines": "1", "note": "Protocol fixture"}],
             "decisions": [{"id": "D1", "what": "No edit", "why": "Protocol-only test"}],
             "steps": [{"id": "S1", "do": "Inspect fixture", "verify": "unit"}],
             "expectedScope": ["value.txt"], "acceptance": ["unit"], "mayAdjust": [], "escalateWhen": []}
    outcome = {"status": "completed", "changed": [], "checks": [], "residual": [],
               "failedStep": "", "decisionNeeded": "", "reason": "", "blockedReason": "", "attempted": [], "evidence": []}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            seen.append({"model": body.get("model"), "path": self.path,
                         "has_schema": bool(body.get("text", {}).get("format", {}).get("schema")),
                         "max_output_tokens": body.get("max_output_tokens"),
                         "request_bytes": len(raw)})
            if body.get("text", {}).get("format", {}).get("type") == "json_schema":
                error = b'{"error":{"code":"unsupported_parameter","message":"json_schema unsupported"}}'
                self.send_response(400)
                self.send_header("Content-Length", str(len(error)))
                self.end_headers()
                self.wfile.write(error)
                return
            text = json.dumps(draft if body.get("model") == "gpt-6.1-sol" else outcome)
            content = {"type": "output_text", "text": text, "annotations": []}
            item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed", "content": [content]}
            response = {"id": "resp_fixture", "object": "response", "created_at": int(time.time()), "model": body["model"],
                        "status": "completed", "output": [item],
                        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                                  "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": {**content, "text": ""}},
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text},
                {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "text": text},
                {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": content},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response}]
            data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = json.loads((ROOT / "config.example.json").read_text())
    local_config = ROOT / "config.local.json"
    config["codex_command"] = (read_config(local_config)["codex_command"] if local_config.exists()
                               else [shutil.which("codex") or "codex"])
    config["checks"] = {"unit": [sys.executable, "-c", "from pathlib import Path; assert Path('value.txt').read_text()=='fixture'"]}
    for role in ("planner", "worker"):
        config[role]["endpoint"] = {"base_url": f"http://127.0.0.1:{server.server_port}/v1"}
        config[role]["timeout_seconds"] = 90
        config[role]["max_requests"] = 2
    atomic_json(parent / "config.json", config)
    try:
        if args.plugin:
            from relaylib.plugin import JobStore
            store = JobStore(parent / "config.json", parent / "jobs", [project])
            job = store.submit(str(project), "inspect-v1", {
                "goal": "Offline protocol test; no edits or tools. Return the required JSON.",
                "files": [{"path": "value.txt", "note": "Contains fixture"}], "steps": ["Report completion without editing"]})
            value = store.wait(job["job_id"], seconds=50)
            for child in store.children:
                child.wait(timeout=5)
            atomic_json(parent / "observed.json", {"requests": seen, "result": value, "mode": "LOCALHOST_FIXTURE_NO_PAID_MODELS"})
            print(json.dumps({"status": value["status"], "observed": seen, "artifacts": str(parent)}, ensure_ascii=False))
            return 0 if value["status"] == "completed" and len(seen) == 1 and not seen[0]["has_schema"] and seen[0]["max_output_tokens"] is None else 1
        state = Runner(read_config(parent / "config.json"), project, parent / "relay").start("Offline protocol test; no tools, no edits. Return the requested JSON structure.")
        atomic_json(parent / "observed.json", {"requests": seen, "mode": "LOCALHOST_FIXTURE_NO_PAID_MODELS"})
        print(json.dumps({"status": state["status"], "failure": state["failure"], "observed": seen, "artifacts": str(parent)}, ensure_ascii=False))
        return 0 if state["status"] == "completed" and len(seen) == 2 and not any(s["has_schema"] for s in seen) else 1
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
