"""End-to-end check of the follow-up report with a real native Codex session.

The candidate is rebuilt from a fixed commit and run against an isolated data dir
on its own port. The project keeps the same role mapping as the real one (reporter
= codex), so the member that writes the report is the app's own Codex agent, not a
script: this script only seeds the project, clicks the panel's request, then runs
one real `codex exec` turn whose MCP tool list is the packaged bridge. The body it
submits is whatever that turn writes.

Nothing global is touched: `-c` overrides carry the bridge entry, and HOME is
redirected so no ~/.codex/config.toml is read or written.
"""
import json
import os
import subprocess
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4441
ROOT = os.path.abspath(".")
DATA = os.path.abspath(r"qa\report-native\data")
WORKSPACE = os.path.abspath(r"qa\report-native\workspace")
BRIDGE = os.path.abspath(r"release\StageRelay-0.1.4-report\Stage Relay\StageRelayBridge.exe")
CODEX = (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
         r"\app\resources\codex.exe")
BASE = f"http://127.0.0.1:{PORT}"


def panel():
    request = urllib.request.Request(BASE + "/")
    with urllib.request.urlopen(request) as response:
        return response.headers.get("Set-Cookie", "").split(";")[0]


COOKIE = panel()


def post(action, **args):
    payload = json.dumps({"action": action, "args": args}, ensure_ascii=False).encode()
    request = urllib.request.Request(BASE + "/api/action", data=payload, headers={
        "Content-Type": "application/json", "X-Relay-Client": "panel", "Cookie": COOKIE})
    with urllib.request.urlopen(request) as response:
        return json.load(response).get("result", {})


def state():
    request = urllib.request.Request(BASE + "/api/state")
    request.add_header("Cookie", COOKIE)
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def bridge(agent, calls):
    """One bridge process, several tool calls, the way a native turn uses it."""
    messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                            "clientInfo": {"name": "probe", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    for index, (name, arguments) in enumerate(calls, start=2):
        messages.append({"jsonrpc": "2.0", "id": index, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
    result = subprocess.run([BRIDGE, "--agent", agent, "--port", str(PORT), "--data-dir", DATA],
                            input="\n".join(json.dumps(m) for m in messages) + "\n",
                            text=True, encoding="utf-8", capture_output=True)
    out = {}
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message.get("id"), int) and message["id"] > 1:
            out[message["id"] - 2] = message["result"]
    return out


def main():
    project = post("project_create", title="跟进汇报原生闭环", goal="真实原生会话写作",
                   workspace=WORKSPACE)
    pid = project["id"]
    post("assign", project_id=pid,
         assignments={"planner": "codex", "executor": "deepseek", "reporter": "codex"})
    post("task_create", project_id=pid, title="报表导出", owner="deepseek",
         acceptance="导出文件可读且字段完整")
    # A board deliberately left open: a follow-up report must not wait for it.
    request = post("report_request", project_id=pid, request_id="native-e2e-1")
    print("request", request["id"], request["status"], "covered_to", request["covered_to"])

    events = [e for e in state()["events"] if e["detail"].get("report_id") == request["id"]]
    print("wake recipients", events[-1]["detail"]["recipients"])

    prompt = (
        "你是 Stage Relay 项目里 codex 成员的汇总负责人。只使用 stage-relay-codex 开头的 MCP 工具。\n"
        f"1. 调 relay_inbox，找到带有请求号 native-e2e-1 的跟进汇报请求。\n"
        f"2. 用 relay_project 读取项目 {pid} 的实际状态（板块、议题、事件），不要凭空编造。\n"
        "3. 按固定结构写报告并调用 relay_follow_up_report 提交，参数 project_id 与 request_id 与上面一致，"
        "body 里必须分别写清：方案与执行会话对应、上次汇报以来已改与未改、验证与部署状态、阻碍、"
        "下一步建议（明确标为建议，由用户决定）。\n"
        "4. 最后用一行 JSON 输出 {\"submitted\":true/false,\"request_id\":\"…\",\"body_len\":N}"
    )
    command = [CODEX, "exec", "-C", WORKSPACE, "--skip-git-repo-check", "--json",
               "-c", f'mcp_servers.stage-relay-codex.command="{BRIDGE}"',
               "-c", 'mcp_servers.stage-relay-codex.args=[' + ",".join(
                   json.dumps(a) for a in ["--agent", "codex", "--port", str(PORT),
                                           "--data-dir", DATA]) + ']',
               "-c", 'mcp_servers.stage-relay-codex.enabled=true',
               prompt]
    env = dict(os.environ, HOME=os.path.abspath(r"qa\report-native\home"),
               USERPROFILE=os.path.abspath(r"qa\report-native\home"))
    run = subprocess.run(command, capture_output=True, text=True, timeout=900,
                         encoding="utf-8", errors="replace", env=env)
    log = os.path.abspath(r"qa\report-native\codex-native.log")
    with open(log, "w", encoding="utf-8") as handle:
        handle.write(run.stdout)
        handle.write("\n--- stderr ---\n")
        handle.write(run.stderr)
    print("codex exit", run.returncode, "log", log)

    project_state = [p for p in state()["projects"] if p["id"] == pid][0]
    record = next((r for r in project_state["reports"] if r["request_id"] == "native-e2e-1"), None)
    print("record status", record and record["status"])
    print("record body length", len(record["body"]) if record else 0)
    if record and record["body"]:
        print("record body head", record["body"][:200].replace("\n", " | "))
    print("PROJECT", pid)
    return 0 if record and record["status"] == "submitted" else 1


if __name__ == "__main__":
    sys.exit(main())