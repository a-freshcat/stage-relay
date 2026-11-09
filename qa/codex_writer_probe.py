"""Reproduce the Codex 'active writer' condition against a real app-server.

Two things this settles, because the answer decides the whole fix:

  * whether `thread/resume` on a running thread *rejoins* (the documented
    behaviour) or is refused with an active-writer error, and
  * whether a second app-server process can resume a thread the first one holds.

Only threads created here are touched; the real bound thread is never used.
"""
import json
import os
import subprocess
import sys
import threading
import time

CODEX = (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
         r"\app\resources\codex.exe")
WORKSPACE = os.path.abspath(r"qa\codex-writer-probe")
os.makedirs(WORKSPACE, exist_ok=True)


class Server:
    def __init__(self, label):
        self.label = label
        self.proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.next_id = 0
        self.responses = {}
        self.lock = threading.Lock()
        self.errors = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "writer-probe", "version": "1"}})

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message and "method" not in message:
                with self.lock:
                    self.responses[message["id"]] = message
            elif "error" in message:
                self.errors.append(message)

    def _read_err(self):
        for line in self.proc.stderr:
            if line.strip():
                self.errors.append({"stderr": line.strip()[:300]})

    def request(self, method, params, timeout=25):
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
        self.proc.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                found = self.responses.pop(request_id, None)
            if found:
                return found
            time.sleep(0.05)
        raise TimeoutError(f"{method} timed out on {self.label}")

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {str(detail)[:400]}"))


first = Server("first")
try:
    started = first.request("thread/start", {"cwd": WORKSPACE, "sandbox": "workspace-write",
                                             "approvalPolicy": "on-request",
                                             "serviceName": "stage-relay-probe",
                                             "ephemeral": False})
    thread_id = started.get("result", {}).get("thread", {}).get("id")
    check("在隔离目录创建 thread", bool(thread_id), started)
    # Keep a turn alive so the thread is genuinely running.
    turn = first.request("turn/start", {"threadId": thread_id,
                                        "input": [{"type": "text", "text": "sleep briefly",
                                                   "text_elements": []}]})
    check("thread 处于运行中", "result" in turn, turn)
    # Same process re-resuming a running thread: documented as "rejoins".
    again = first.request("thread/resume", {"threadId": thread_id, "excludeTurns": True,
                                            "cwd": WORKSPACE})
    rejoined = "result" in again
    check("同进程 resume 运行中 thread 为重新加入", rejoined,
          again.get("error") or again.get("result", {}).get("thread", {}).get("id"))

    # A second app-server against the same thread is the real contention case.
    second = Server("second")
    try:
        other = second.request("thread/resume", {"threadId": thread_id, "excludeTurns": True,
                                                 "cwd": WORKSPACE})
        message = json.dumps(other, ensure_ascii=False)
        check("第二进程 resume 的真实结果", True, message[:400])
        print("   second result:", message[:400])
        print("   second stderr:", json.dumps(second.errors[-3:], ensure_ascii=False)[:400])
    finally:
        second.close()
finally:
    first.close()

print(json.dumps({"results": [(n, ok) for n, ok, _ in results]}, ensure_ascii=False))
