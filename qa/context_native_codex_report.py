"""Complete the native loop: a board owned by codex so relay_report is allowed.

The previous native turn proved the wake and the ack protocol from inside a real
Codex session, but its report was refused because the board belonged to
deepseek — correct behaviour, worth keeping. This run adds a codex-owned board so
the native agent can actually deliver evidence, then verifies the review side.
"""
import json
import os
import subprocess
import sys
import urllib.request

CODEX = (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
         r"\app\resources\codex.exe")
BRIDGE = os.path.abspath(r"release\StageRelay-0.1.2\Stage Relay\StageRelayBridge.exe")
DATA = os.path.abspath(r"qa\context-native-a1b2c3\data")
WORKSPACE = os.path.abspath(r"qa\context-native-a1b2c3\workspace")
BASE = "http://127.0.0.1:4488"


def panel(path, payload=None, cookie=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Relay-Client", "panel")
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r), r.headers.get("Set-Cookie", "").split(";")[0]


with urllib.request.urlopen(BASE + "/", timeout=15) as page:
    cookie = page.headers.get("Set-Cookie", "").split(";")[0]
state, _ = panel("/api/state", cookie=cookie)
project = [p for p in state["projects"] if "隔离" in p["title"]][0]["id"]

# A codex-owned board: the native agent is a real executor here, not a bystander.
created, _ = panel("/api/action", {"action": "task_create", "args": {
    "project_id": project, "title": "原生 Codex 自证板块",
    "acceptance": "原生会话用自身工具读取并按需回读，然后 relay_report",
    "owner": "codex"}}, cookie=cookie)
task_id = created["result"]["id"]
panel("/api/action", {"action": "dispatch", "args": {"project_id": project}}, cookie=cookie)
print("codex-owned board", task_id)

prompt = f"""你在 Stage Relay 隔离验证中扮演 codex 成员，只使用 stage-relay-codex 的 MCP 工具。

任务：
1. 调 relay_inbox，记下 from_seq 与 next_ack。
2. 再调一次 relay_inbox（不带 ack），确认 from_seq 与事件批次是否相同。
3. 用 relay_task 按需读取板块 {task_id}，确认 acceptance 原文能完整读回。
4. 用 relay_report 提交板块 {task_id} 的完成汇报，evidence 里必须写：两次 from_seq 数值、批次是否相同、next_ack 数值、acceptance 原文片段。这个板块 owner 是 codex，你应当有权限。

最后输出一行 JSON：{{"first_from_seq":…,"second_from_seq":…,"same_batch":true/false,"next_ack":…,"acceptance_read":true/false,"report_ok":true/false}}"""

command = [
    CODEX, "exec", "-C", WORKSPACE, "--skip-git-repo-check", "--json",
    "-c", f'mcp_servers.stage-relay-codex.command="{BRIDGE}"',
    "-c", 'mcp_servers.stage-relay-codex.args=[' + ",".join(
        json.dumps(a) for a in ["--agent", "codex", "--port", "4488", "--data-dir", DATA]) + ']',
    "-c", 'mcp_servers.stage-relay-codex.enabled=true',
    prompt,
]
result = subprocess.run(command, capture_output=True, text=True, timeout=900,
                        encoding="utf-8", errors="replace")
with open(os.path.abspath(r"qa\context-native-a1b2c3\codex-native-2.log"), "w", encoding="utf-8") as fh:
    fh.write(result.stdout + "\n--- stderr ---\n" + result.stderr)
print("exit", result.returncode)
for line in result.stdout.splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        event = json.loads(line)
    except ValueError:
        continue
    item = event.get("item") or {}
    if item.get("type") == "agent_message" and item.get("text"):
        print("##", item["text"][:600])

# The board must now be in review with real evidence written by the native agent.
after, _ = panel("/api/state", cookie=cookie)
board = [t for p in after["projects"] if p["id"] == project for t in p["tasks"] if t["id"] == task_id]
if board:
    print("status", board[0]["status"])
    print("evidence", board[0]["evidence"][:400])
sys.exit(0 if result.returncode == 0 else 1)
