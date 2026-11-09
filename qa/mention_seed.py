"""Seed a mention fixture on an isolated panel and leave it open for the browser.

The verification above already proves the server side; this prepares a project with
a fresh unread mention so the page can be checked by hand and by the browser lane.

Usage: python qa/mention_seed.py <base-url> <data-dir> [version]
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = sys.argv[1]
DATA = Path(sys.argv[2])
VERSION = sys.argv[3] if len(sys.argv) > 3 else "0.1.2"
PORT = BASE.rsplit(":", 1)[1]
BRIDGE = ROOT / f"release/StageRelay-{VERSION}/Stage Relay/StageRelayBridge.exe"


def panel(path, body=None, cookie=None):
    request = urllib.request.Request(BASE + path)
    if cookie:
        request.add_header("Cookie", cookie)
    if body is not None:
        request.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Relay-Client", "panel")
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        fresh = response.headers.get("Set-Cookie", "").split(";")[0]
    if body is None and path == "/":
        return {}, fresh
    return (json.loads(raw) if raw else {}), fresh


_, cookie = panel("/")


def act(action, **args):
    payload, _ = panel("/api/action", {"action": action, "args": args}, cookie)
    return payload.get("result", payload)


def bridge(agent, calls):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": "mention-seed", "version": "1"}}}, ensure_ascii=False),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
    for index, (name, args) in enumerate(calls, start=2):
        lines.append(json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}, ensure_ascii=False))
    subprocess.run([str(BRIDGE), "--agent", agent, "--port", PORT, "--data-dir", str(DATA)],
                   input="\n".join(lines) + "\n", text=True, encoding="utf-8",
                   capture_output=True, timeout=120)


project = act("project_create", title="提及提醒界面验证", goal="用真实页面核对红点、定位与清除",
              workspace=str(DATA.parent))
pid = project["id"]
act("assign", project_id=pid,
    assignments={"planner": "codex", "executor": "deepseek", "reporter": "codex"})
first = act("issue_create", project_id=pid, title="需要你确认的议题",
            body="成员会在这里汇报并点名用户", members=["codex", "deepseek"], task_ids=[])
second = act("issue_create", project_id=pid, title="不需要打扰你的议题",
            body="这里只讨论执行细节", members=["codex", "deepseek"], task_ids=[])

# The mention is the OLDEST message and a long tail follows it, so opening the
# thread scrolls to the bottom and the mention sits outside the visible area: the
# count must survive that, and only clear once it is scrolled into view.
bridge("codex", [("relay_send_message",
                  {"project_id": pid, "issue_id": first["id"],
                   "body": "@user 阶段依据已齐，请你确认后我再汇总。"})])
for n in range(10):
    bridge("deepseek", [("relay_send_message",
                         {"project_id": pid, "issue_id": first["id"],
                          "body": f"后续进度第 {n + 1} 条，" + "细节。" * 40})])
bridge("deepseek", [("relay_send_message",
                     {"project_id": pid, "issue_id": second["id"],
                      "body": "@deepseek 只写给执行成员。"})])

payload, _ = panel("/api/state", None, cookie)
target = next(p for p in payload["projects"] if p["id"] == pid)
print(json.dumps({"project": pid, "issue": first["id"], "other_issue": second["id"],
                  "mention_counts": target["mention_counts"],
                  "mention_total": target["mention_total"]}, ensure_ascii=False, indent=1))