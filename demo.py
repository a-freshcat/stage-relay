#!/usr/bin/env python3
"""Create a disposable Git demo and run a mock (default) or explicit live relay."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from relaylib.runner import ROOT, Runner, read_config
from relaylib.runtime import atomic_json


def prepare():
    parent = ROOT / "runs" / ("demo-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    project = parent / "project"
    project.mkdir(parents=True)
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (project / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass TestAdd(unittest.TestCase):\n"
        "    def test_positive(self):\n        self.assertEqual(add(3, 2), 5)\n"
        "    def test_negative(self):\n        self.assertEqual(add(-3, -2), -5)\n", encoding="utf-8")
    (project / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "calc.py", "test_calc.py", ".gitignore"],
                 ["-c", "user.name=Relay demo", "-c", "user.email=relay@example.invalid", "commit", "-qm", "Demo baseline"]):
        subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config["codex_command"] = [shutil.which("codex") or "codex"]
    config["checks"] = {"unit": [sys.executable, "-m", "unittest", "-v"]}
    request = "修复 calc.py 的 add，使正数和负数相加正确。不要修改测试，保持函数接口。运行已有单元测试。"
    (parent / "request.md").write_text(request, encoding="utf-8")
    return parent, project, config, request


def main():
    parser = argparse.ArgumentParser(description="Default is offline mock, not a paid model call")
    parser.add_argument("--live", action="store_true", help="Explicitly call configured Sol + DeepSeek models")
    parser.add_argument("--prepare-only", action="store_true", help="Create the demo without running it")
    parser.add_argument("--native-auth", action="store_true", help="Use native Codex login passthrough to loopback CCSwitch")
    args = parser.parse_args()
    parent, project, config, request = prepare()
    if args.native_auth:
        for role in ("planner", "worker"):
            config[role]["endpoint"]["native_auth"] = True
    if args.prepare_only:
        atomic_json(parent / "config.json", config)
        print(json.dumps({"project": str(project), "config": str(parent / "config.json"),
                          "request": str(parent / "request.md"), "run_dir": str(parent / "relay")}, ensure_ascii=False))
        return 0
    server = None
    if not args.live:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = b'{"mock":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        config["codex_command"] = [sys.executable, str(ROOT / "tests" / "fake_codex.py")]
        for role in ("planner", "worker"):
            config[role]["endpoint"] = {"base_url": f"http://127.0.0.1:{server.server_port}/v1"}
        draft = {"goal": "修复 add", "codeFacts": [{"path": "calc.py", "symbol": "add", "lines": "1-2", "note": "a-b 错误，应为 a+b；已有正负数单元测试。"}],
                 "decisions": [{"id": "D1", "what": "只修正运算符", "why": "保持接口和测试不变"}],
                 "steps": [{"id": "S1", "do": "把减法改为加法", "verify": "unit"}],
                 "expectedScope": ["calc.py"], "acceptance": ["unit"], "mayAdjust": [], "escalateWhen": []}
        outcome = {"status": "completed", "answer": "已修正 add 的加法运算，保留接口与测试。", "changed": [{"path": "calc.py", "summary": "改为加法"}], "checks": [],
                   "residual": [], "failedStep": "", "decisionNeeded": "", "reason": "", "blockedReason": "", "attempted": [], "evidence": []}
        sequence = parent / "mock-sequence.json"
        review = {"status": "approved", "summary": "已核对：仅将减法改为加法，已有正负数单元测试通过。",
                  "evidence": ["calc.py 使用 a + b；unit 检查退出码为 0。"], "issues": []}
        atomic_json(sequence, {"cursor": 0, "entries": [{"response": draft}, {"response": outcome, "write": {"calc.py": "def add(a, b):\n    return a + b\n"}}, {"response": review}]})
        os.environ["RELAY_MOCK_SEQUENCE"] = str(sequence)
    atomic_json(parent / "config.json", config)
    try:
        state = Runner(read_config(parent / "config.json"), project, parent / "relay").start(request)
        print(json.dumps({"mode": "LIVE" if args.live else "MOCK_NO_PAID_CALLS", "status": state["status"],
                          "failure": state["failure"], "artifacts": str(parent / "relay")}, ensure_ascii=False))
        return 0 if state["status"] == "completed" else 2
    finally:
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
