"""Real-page verification of the mention notice for the user.

Runs against a live isolated panel over HTTP, using the packaged bridge for the
member side, then asserts what the browser would show:

1. a member writes a report addressed to the user -> it is counted, not lost;
2. a message that only talks about the token in code is not counted;
3. the count survives a reload (it comes from the server, not the panel);
4. clearing one issue leaves the other issue untouched;
5. a mention never counts itself when the user writes it.

Usage: python qa/mention_verify.py <base-url> <data-dir> [version]
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
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {name} | {detail}")


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
        cookie = response.headers.get("Set-Cookie", "").split(";")[0]
    if not body and path == "/":
        return {}, cookie
    return (json.loads(raw) if raw else {}), cookie


_, cookie = panel("/")
def act(action, **args):
    payload, _ = panel("/api/action", {"action": action, "args": args}, cookie)
    return payload.get("result", payload)


def state():
    payload, _ = panel("/api/state", None, cookie)
    return payload


def bridge(agent, calls):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": "mention-verify", "version": "1"}}}, ensure_ascii=False),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})]
    for index, (name, args) in enumerate(calls, start=2):
        lines.append(json.dumps({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}, ensure_ascii=False))
    run = subprocess.run([str(BRIDGE), "--agent", agent, "--port", PORT, "--data-dir", str(DATA)],
                         input="\n".join(lines) + "\n", text=True, encoding="utf-8",
                         capture_output=True, timeout=120)
    replies = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    out = []
    for reply in replies:
        if reply.get("id", 0) >= 2:
            result = reply.get("result", {})
            text = result.get("content", [{}])[0].get("text", "")
            out.append((bool(result.get("isError")), text))
    return out


def project_of(payload, project_id):
    return next(p for p in payload["projects"] if p["id"] == project_id)


# --- fixture: a real project with two members and two issues -------------------
project = act("project_create", title="提及提醒验证", goal="验证提到用户的未读提醒",
              workspace=str(DATA.parent))
pid = project["id"]
act("assign", project_id=pid,
    assignments={"planner": "codex", "executor": "deepseek", "reporter": "codex"})
first = act("issue_create", project_id=pid, title="第一个议题",
            body="成员会在这里汇报", members=["codex", "deepseek"], task_ids=[])
second = act("issue_create", project_id=pid, title="第二个议题",
             body="另一个议题也要分开计数", members=["codex", "deepseek"], task_ids=[])

# --- 1. a member-side report that mentions the user is counted -----------------
sent = bridge("deepseek", [("relay_send_message",
                            {"project_id": pid, "issue_id": first["id"],
                             "body": "@user 交付依据已补齐，请确认是否继续。"})])
check("成员通过原生桥发出汇报", sent and not sent[0][0], sent[0][1][:120] if sent else "no reply")
after_send = state()
check("提到用户的汇报计入未读", project_of(after_send, pid)["mention_counts"].get(first["id"]) == 1,
      project_of(after_send, pid)["mention_counts"])

# --- 2. a code sample that only talks about the token is not counted ------------
bridge("deepseek", [("relay_send_message",
                     {"project_id": pid, "issue_id": first["id"],
                      "body": "写法示例：`@user` 只是代码里的文字。"})])
after_code = project_of(state(), pid)
check("代码里的 @user 不算提及", after_code["mention_counts"].get(first["id"]) == 1,
      after_code["mention_counts"])

# --- an ordinary member-only message stays uncounted ---------------------------
bridge("codex", [("relay_send_message",
                  {"project_id": pid, "issue_id": second["id"],
                   "body": "@deepseek 这条只给执行成员。"})])
check("只写给成员的汇报不提醒用户",
      not project_of(state(), pid)["mention_counts"].get(second["id"]),
      project_of(state(), pid)["mention_counts"])

# --- 3. the count survives a reload: it is read back from the server -----------
reloaded = project_of(state(), pid)
check("刷新后未读仍在", reloaded["mention_counts"].get(first["id"]) == 1, reloaded["mention_counts"])
check("未读总数一致", reloaded["mention_total"] == 1, reloaded["mention_total"])

# --- 4. a second issue mentioning the user is counted separately ---------------
bridge("codex", [("relay_send_message",
                  {"project_id": pid, "issue_id": second["id"],
                   "body": "@我 另一个议题的汇报，等你确认。"})])
both = project_of(state(), pid)
check("两个议题分别计数",
      both["mention_counts"].get(first["id"]) == 1 and both["mention_counts"].get(second["id"]) == 1,
      both["mention_counts"])

# clearing one issue must leave the other alone
cleared = act("mentions_read", project_id=pid, issue_id=first["id"])
after_clear = project_of(state(), pid)
check("清除一个议题只清自己", not after_clear["mention_counts"].get(first["id"]),
      after_clear["mention_counts"])
check("另一个议题仍然未读", after_clear["mention_counts"].get(second["id"]) == 1,
      after_clear["mention_counts"])
check("清除动作报告了被清条目", len(cleared.get("cleared", [])) == 1, cleared)

# --- 5. the user's own message never marks itself unread ----------------------
act("message", project_id=pid, issue_id=second["id"], body="@user 我自己写的提醒")
self_note = project_of(state(), pid)
check("用户自己的发言不计未读", self_note["mention_counts"].get(second["id"]) == 1,
      self_note["mention_counts"])

# --- project-level chat is tracked under its own key --------------------------
bridge("deepseek", [("relay_project_message",
                     {"project_id": pid, "body": "@user 项目级汇报，请查看。"})])
project_chat = project_of(state(), pid)
check("项目群聊的提及单独计数", project_chat["mention_counts"].get("project") == 1,
      project_chat["mention_counts"])
act("mentions_read", project_id=pid, message_ids=[
    entry["message"] for entry in project_chat.get("user_mentions", [])
    if entry.get("issue_id") is None])
check("按消息清除只影响那一条",
      not project_of(state(), pid)["mention_counts"].get("project"),
      project_of(state(), pid)["mention_counts"])

# The fixture is a test fixture: it is closed and recycled, never left as a real task.
act("project_close", project_id=pid, retention_days=1)
act("project_forget", project_id=pid)
remaining = [p["id"] for p in state()["projects"]]
check("验证夹具已回收", pid not in remaining, remaining)

failed = [r for r in results if not r["ok"]]
record = ROOT / "qa" / "mention-verify.json"
record.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({"total": len(results), "failed": len(failed),
                  "failures": [r["check"] for r in failed]}, ensure_ascii=False, indent=1))
sys.exit(1 if failed else 0)