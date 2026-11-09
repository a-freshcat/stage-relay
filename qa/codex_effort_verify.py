"""Verify the Codex effort chain against a real app-server.

Checks the three things the panel now relies on, all from the server itself:
  * `model/list` advertises `supportedReasoningEfforts` + `defaultReasoningEffort`
  * `turn/start` accepts an advertised effort
  * `turn/start` is refused for an effort the model does not advertise

Only a thread created here is used; no real project thread is touched.
"""
import json
import os
import subprocess
import threading
import time

CODEX = (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
         r"\app\resources\codex.exe")
WORKSPACE = os.path.abspath(r"qa\codex-effort-verify")
os.makedirs(WORKSPACE, exist_ok=True)
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {str(detail)[:300]}"))


class Server:
    def __init__(self):
        self.proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1)
        self.inflight = {}
        self.lock = threading.Lock()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "effort-verify", "version": "1"}})

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message:
                with self.lock:
                    self.inflight[message["id"]] = message

    def request(self, method, params, timeout=40):
        with self.lock:
            self.counter += 1
            request_id = self.counter
        self.proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                found = self.inflight.pop(request_id, None)
            if found:
                return found
            time.sleep(0.05)
        raise TimeoutError(method)

    def close(self):
        self.proc.terminate()


server = Server()
try:
    listing = server.request("model/list", {"limit": 100, "includeHidden": False})
    models = listing.get("result", {}).get("data", [])
    with_efforts = [m for m in models if m.get("supportedReasoningEfforts")]
    check("Codex 目录带 supportedReasoningEfforts", bool(with_efforts),
          [m.get("model") for m in models[:4]])
    if not with_efforts:
        raise SystemExit(1)
    sample = with_efforts[0]
    efforts = [e["reasoningEffort"] for e in sample["supportedReasoningEfforts"]]
    print("   model:", sample["model"], "efforts:", efforts,
          "default:", sample.get("defaultReasoningEffort"))

    started = server.request("thread/start", {"cwd": WORKSPACE, "sandbox": "workspace-write",
                                              "approvalPolicy": "on-request",
                                              "serviceName": "stage-relay-verify",
                                              "ephemeral": False})
    thread_id = started.get("result", {}).get("thread", {}).get("id")
    check("创建隔离 thread", bool(thread_id), started)

    good = server.request("turn/start", {"threadId": thread_id, "model": sample["model"],
                                         "effort": efforts[0],
                                         "input": [{"type": "text", "text": "say ok",
                                                    "text_elements": []}]})
    check("受理模型自报的思考深度", "result" in good,
          (good.get("error") or {}).get("message"))
    # A value the model never advertised must be refused, not silently accepted.
    bad = server.request("turn/start", {"threadId": thread_id, "model": sample["model"],
                                        "effort": "definitely-not-an-effort",
                                        "input": [{"type": "text", "text": "say ok",
                                                   "text_elements": []}]})
    check("拒绝未自报的思考深度", "error" in bad or "result" not in bad,
          json.dumps(bad, ensure_ascii=False)[:300])
finally:
    server.close()

print(json.dumps({"results": [(n, ok) for n, ok, _ in results]}, ensure_ascii=False))
