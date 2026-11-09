"""Read-only diagnostic: what the real desktop profile advertises for depth.

Uses the installed app-server / DSH runtime only. Nothing is written, no session
is created, and the real bound thread is never resumed — this only asks each
application what its own model routes support, which is what the panel now shows.
"""
import json
import os
import subprocess
import threading
import time

CODEX = (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
         r"\app\resources\codex.exe")


class Server:
    def __init__(self):
        self.proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1)
        self.inflight = {}
        self.lock = threading.Lock()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "depth-readonly", "version": "1"}})

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
    data = listing.get("result", {}).get("data", [])
    print(f"Codex 桌面目录：{len(data)} 个模型")
    for entry in data:
        efforts = [e.get("reasoningEffort") for e in (entry.get("supportedReasoningEfforts") or [])]
        print(f"  {entry.get('model'):22} 默认={entry.get('defaultReasoningEffort')} "
              f"可选={efforts}")
finally:
    server.close()
