"""Dependency-free MCP stdio bridge (2025-11-25 lifecycle, NDJSON).

The launch arguments bind this process to one member. No tool lets an agent
impersonate a user, approve decisions, reassign work, or execute shell commands.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from . import __version__


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STR = {"type": "string"}
PROJECT = {"project_id": STR}
TOOLS = [
    {"name": "relay_inbox", "description": "读取分配给当前成员的任务、协作消息与用户决定。先读取再行动；paused 时停止推进。保留 cursor 供下一次 after 使用。", "inputSchema": schema({"after": {"type": "integer", "minimum": 0}})},
    {"name": "relay_report", "description": "汇报自己负责且已派发的板块进展。review 表示待核验，必须提供文件改动、命令和实际检查结果。不能自行标记验收通过。", "inputSchema": schema(PROJECT | {"task_id": STR, "status": {"enum": ["running", "review"]}, "evidence": STR}, ["project_id", "task_id", "status", "evidence"])},
    {"name": "relay_raise_issue", "description": "遇到阻碍、分歧或新提案时建立议题并邀请相关成员。列出 task_ids 只暂停受影响板块；需用户决定时提供至少两个带影响说明的选项。", "inputSchema": schema(PROJECT | {"title": STR, "body": STR, "members": {"type": "array", "items": STR}, "task_ids": {"type": "array", "items": STR}, "options": {"type": "array", "maxItems": 4, "items": schema({"title": STR, "detail": STR}, ["title", "detail"])}}, ["project_id", "title", "body"])},
    {"name": "relay_send_message", "description": "向自己参加的议题群聊发送消息，其他成员可从信箱读取。发消息不等于对方已读或已运行。不要代替用户作决定。", "inputSchema": schema(PROJECT | {"issue_id": STR, "body": STR}, ["project_id", "issue_id", "body"])},
    {"name": "relay_project_message", "description": "向项目级群聊发送消息：没有议题时也能说明进度、提问或交接；只对当前项目成员可见，项目结束后只读。发消息不等于对方已运行。", "inputSchema": schema(PROJECT | {"body": STR}, ["project_id", "body"])},
    {"name": "relay_review", "description": "方案监督负责人核验其他成员的待审核板块。提供独立检查依据；不能核验自己的执行结果。分工切换后须从最新项目读取 review_epoch 并随请求提交。", "inputSchema": schema(PROJECT | {"task_id": STR, "approved": {"type": "boolean"}, "evidence": STR, "review_epoch": {"type":"integer","minimum":0}}, ["project_id", "task_id", "approved", "evidence"])},
    {"name": "relay_submit_summary", "description": "当前汇总负责人在用户派发汇总、所有板块核验通过且议题解决后，提交有依据的最终说明。", "inputSchema": schema(PROJECT | {"body": STR}, ["project_id", "body"])},
]
for tool in TOOLS:
    tool['annotations']={'readOnlyHint':tool['name']=='relay_inbox','destructiveHint':False,'openWorldHint':False}
ACTIONS = {"relay_report": "report", "relay_raise_issue": "issue_create", "relay_send_message": "message", "relay_project_message": "project_message", "relay_review": "review", "relay_submit_summary": "summary_submit"}


class Bridge:
    def __init__(self, agent, port, data_dir):
        self.agent = agent
        self.port = port
        self.data_dir = Path(data_dir)
        self.initialized = False
        self.ready = False
        self.session = "mcp_" + secrets.token_hex(6)

    def request(self, action, args):
        credentials = json.loads((self.data_dir / "bridge-credentials.json").read_text(encoding="utf-8"))
        token = credentials.get(self.agent)
        if not token:
            raise ValueError("成员尚未登记，请在面板中创建并复制接入配置。")
        body = json.dumps({"action": action, "args": args}, ensure_ascii=False).encode("utf-8")
        request = Request(f"http://127.0.0.1:{self.port}/bridge", data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)["result"]
        except HTTPError as exc:
            raise ValueError(json.load(exc).get("error", "面板拒绝此操作。")) from None
        except URLError:
            raise ValueError("无法连接面板，请先启动 Stage Relay。") from None

    def handle(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        request_id = request.get("id")
        method = request["method"]
        params = request.get("params", {})
        if method == "notifications/initialized" and self.initialized:
            self.ready = True
            return None
        if "id" not in request:
            return None
        response = {"jsonrpc": "2.0", "id": request_id}
        if not isinstance(params, dict):
            return response | {"error": {"code": -32602, "message": "Invalid params"}}
        if method == "initialize":
            version = params.get("protocolVersion")
            supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
            self.initialized = True
            self.ready = False
            response["result"] = {"protocolVersion": version if version in supported else "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "stage-relay", "version": __version__}, "instructions": f"当前成员：{self.agent}。先调用 relay_inbox 获取职责与任务，只在项目指定的 workspace 文件夹中工作，不要改变其他任务的会话目录。遇到阻碍先发起议题，用户拥有最终决策权。不要以 API 模型代替原应用工具；协作暂停为合作式通知，必须遵守。协作工具写入信箱；已绑定的原生连接负责送达，不要假设未绑定成员在线。"}
            try:
                self.request("connect", {"session": self.session})
            except (ValueError, OSError):
                # Discovery remains available while the panel is stopped.
                pass
        elif method == "ping":
            response["result"] = {}
        elif not self.ready:
            response["error"] = {"code": -32002, "message": "Initialize and send notifications/initialized first"}
        elif method == "tools/list":
            response["result"] = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            tool = next((t for t in TOOLS if t["name"] == name), None)
            try:
                if not tool or not isinstance(arguments, dict):
                    raise ValueError("工具或参数无效。")
                spec = tool["inputSchema"]
                if set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
                    raise ValueError("参数缺失或包含未支持字段。")
                result = self.request("inbox" if name == "relay_inbox" else ACTIONS[name], arguments)
                response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
            except (ValueError, OSError, KeyError) as exc:
                response["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    args = parser.parse_args()
    bridge = Bridge(args.agent, args.port, args.data_dir)
    for raw in sys.stdin.buffer:
        if len(raw) > 1024 * 1024:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request too large"}}
        else:
            try:
                response = bridge.handle(json.loads(raw.decode("utf-8")))
            except (ValueError, TypeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
