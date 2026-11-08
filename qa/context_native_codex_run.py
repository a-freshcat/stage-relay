"""Drive one real Codex native session against the isolated candidate instance.

The global ~/.codex/config.toml is not touched: the Stage Relay MCP entry and the
model provider are overridden per process with `codex exec -c`. The real project
binding, the published 0.1.1 package and the real data directory stay untouched.

The prompt asks the native Codex agent to use its own Stage Relay tools — read the
inbox, read a task on demand, and report a marker file — so what is verified is a
real native turn consuming a Stage Relay wake, not an HTTP round trip.
"""
import json
import os
import subprocess
import sys

CODEX = (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
         r"\app\resources\codex.exe")
BRIDGE = os.path.abspath(r"release\StageRelay-0.1.2\Stage Relay\StageRelayBridge.exe")
DATA = os.path.abspath(r"qa\context-native-a1b2c3\data")
WORKSPACE = os.path.abspath(r"qa\context-native-a1b2c3\workspace")
LOG = os.path.abspath(r"qa\context-native-a1b2c3\codex-native.log")

prompt = """你在 Stage Relay 隔离验证中扮演 codex 成员。只使用 stage-relay-deepseek 之外的 stage-relay-codex MCP 工具（命名以 stage-relay-codex 开头）。

请依次做四件事，然后简短报告：
1. 调 relay_inbox，记录 from_seq、next_ack 和事件数量。
2. 不做 ack 再调一次 relay_inbox，记录 from_seq 是否与第一次相同、events 是否同一批。
3. 用 relay_list 读取 kind=tasks 的一页（limit=5），记录 total 与返回条数。
4. 在 workspace 写一个文件 native-codex-marker.txt，内容为第一次 relay_inbox 的 next_ack 值，然后调用 relay_report 汇报板块完成，evidence 里写明这个文件路径与上述三个数字。

最后用一行 JSON 输出：{"first_from_seq":…, "second_from_seq":…, "same_batch":true/false, "tasks_total":…, "marker":"…"}"""

command = [
    CODEX, "exec",
    "-C", WORKSPACE,
    "--skip-git-repo-check",
    "--json",
    "-c", f'mcp_servers.stage-relay-codex.command="{BRIDGE}"',
    # `-c` parses the value as TOML, so the array has to be spelled as one.
    "-c", 'mcp_servers.stage-relay-codex.args=[' + ",".join(
        json.dumps(a) for a in ["--agent", "codex", "--port", "4488", "--data-dir", DATA]) + ']',
    "-c", 'mcp_servers.stage-relay-codex.enabled=true',
    prompt,
]

print("command:", command[0], "exec -C", WORKSPACE, "--json ...")
result = subprocess.run(command, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
with open(LOG, "w", encoding="utf-8") as fh:
    fh.write(result.stdout)
    fh.write("\n--- stderr ---\n")
    fh.write(result.stderr)
print("exit", result.returncode, "stdout_len", len(result.stdout))
print(result.stderr[-2000:])
sys.exit(0 if result.returncode == 0 else 1)
