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
from http.client import HTTPConnection, HTTPException
from . import __version__, rules


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STR = {"type": "string"}
INT = {"type": "integer", "minimum": 1}
PROJECT = {"project_id": STR}
TOOLS = [
    {"name": "relay_inbox", "description": "读取分配给当前成员的任务与协作事件。默认只返回项目概要、事件摘要与游标；完整证据、群聊与议题历史用 relay_project / relay_task / relay_issue / relay_message / relay_event / relay_list 按需读取。位置不会自动前进：确认这一批已收到，再传 ack=<next_ack> 推进；不传 ack 会重复返回同一批（宁重复不丢失）。从头分页重读传 full=true 或 after=0。", "inputSchema": schema({"after": {"type": "integer", "minimum": 0}, "full": {"type": "boolean"}, "ack": {"type": "integer", "minimum": 0}})},
    {"name": "relay_rules", "description": "读取固定协作规则原文。会话新建、换绑、上下文压缩或宿主重建后调用一次即可恢复身份与权限约束；不占用每次唤醒。", "inputSchema": schema()},
    {"name": "relay_project", "description": "按需读取一个项目的完整记录：全部板块与证据、议题全部消息、项目群聊全文、决定历史。", "inputSchema": schema(PROJECT, ["project_id"])},
    {"name": "relay_task", "description": "按需读取一个工作板块的完整记录，含执行依据、核验依据与历史核验参考。", "inputSchema": schema(PROJECT | {"task_id": STR}, ["project_id", "task_id"])},
    {"name": "relay_issue", "description": "按需读取一个议题的完整记录，含全部消息、附件与决定。", "inputSchema": schema(PROJECT | {"issue_id": STR}, ["project_id", "issue_id"])},
    {"name": "relay_message", "description": "按需读取单条消息原文（项目群聊或议题消息）；给出 issue_id 时只在该议题内查找。", "inputSchema": schema(PROJECT | {"message_id": STR, "issue_id": STR}, ["project_id", "message_id"])},
    {"name": "relay_list", "description": "分页读取一个项目的完整条目（tasks / issues / chat / messages / events），逐条为全文并返回 total 与 next_offset；取单条时用 relay_task / relay_issue / relay_message 更省。", "inputSchema": schema(PROJECT | {"kind": {"enum": ["tasks", "issues", "chat", "messages", "events"]}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "issue_id": STR}, ["project_id", "kind"])},
    {"name": "relay_event", "description": "按需读取一条协作事件的完整原文（按 seq）。", "inputSchema": schema(PROJECT | {"seq": INT}, ["project_id", "seq"])},
    {"name": "relay_report", "description": "汇报自己负责且已派发的板块进展。review 表示待核验，必须提供文件改动、命令和实际检查结果。不能自行标记验收通过。", "inputSchema": schema(PROJECT | {"task_id": STR, "status": {"enum": ["running", "review"]}, "evidence": STR}, ["project_id", "task_id", "status", "evidence"])},
    {"name": "relay_raise_issue", "description": "遇到阻碍、分歧或新提案时建立议题并邀请相关成员。列出 task_ids 只暂停受影响板块；需用户决定时提供至少两个带影响说明的选项。", "inputSchema": schema(PROJECT | {"title": STR, "body": STR, "members": {"type": "array", "items": STR}, "task_ids": {"type": "array", "items": STR}, "options": {"type": "array", "maxItems": 4, "items": schema({"title": STR, "detail": STR}, ["title", "detail"])}}, ["project_id", "title", "body"])},
    {"name": "relay_send_message", "description": "向自己参加的议题群聊发送消息，其他成员可从信箱读取。发消息不等于对方已读或已运行。不要代替用户作决定。", "inputSchema": schema(PROJECT | {"issue_id": STR, "body": STR}, ["project_id", "issue_id", "body"])},
    {"name": "relay_project_message", "description": "向项目级群聊发送消息：没有议题时也能说明进度、提问或交接；只对当前项目成员可见，项目结束后只读。发消息不等于对方已运行。", "inputSchema": schema(PROJECT | {"body": STR}, ["project_id", "body"])},
    {"name": "relay_review", "description": "方案监督负责人核验其他成员的待审核板块。提供独立检查依据；不能核验自己的执行结果。分工切换后须从最新项目读取 review_epoch 并随请求提交。", "inputSchema": schema(PROJECT | {"task_id": STR, "approved": {"type": "boolean"}, "evidence": STR, "review_epoch": {"type":"integer","minimum":0}}, ["project_id", "task_id", "approved", "evidence"])},
    {"name": "relay_submit_summary", "description": "当前汇总负责人在用户派发汇总、所有板块核验通过且议题解决后，提交有依据的最终说明。", "inputSchema": schema(PROJECT | {"body": STR}, ["project_id", "body"])},
    {"name": "relay_request_summary", "description": "进度监督判断某个阶段完成时，请汇总负责人做阶段汇总。传 issue_id 表示只汇总该议题及其关联板块，可在项目仍在推进时使用；不传则按全项目最终汇总，需所有板块验收且议题解决。只能由当前进度监督或用户发起。若你同时是汇总负责人，这次调用会直接写成该阶段的汇总记录，因此必须带 body（不再要求你先向自己请求）。不能代替用户验收。", "inputSchema": schema(PROJECT | {"issue_id": STR, "body": STR}, ["project_id"])},
    {"name": "relay_follow_up_report", "description": "汇总负责人提交面板请求的跟进汇报。传请求号 request_id（面板点击「生成跟进汇报」后出现在唤醒里）。正文按面板固定结构写：方案与执行会话对应、上次汇报以来的改动与未改动、验证与部署状态、阻碍、下一步建议（建议须标明是建议，由用户决定）。同一请求重复提交只保留第一次正文；生成失败用 status=failed 与 error 说明原因，用户可重试。", "inputSchema": schema(PROJECT | {"request_id": STR, "body": STR, "error": STR, "status": STR}, ["project_id", "request_id"])},
    {"name": "relay_stage_summary", "description": "汇总负责人提交某个阶段范围的汇总。可用 stage_id（relay_request_summary 返回），也可直接传 issue_id 当场为该议题建立并提交阶段记录——兼任汇总负责人时就该用这种方式，无需先请求。同一议题已有汇总时拒绝，返修后由监督发起新的修订。", "inputSchema": schema(PROJECT | {"stage_id": STR, "issue_id": STR, "body": STR}, ["project_id", "body"])},
    {"name": "relay_stages", "description": "读取项目的阶段汇总记录：全部历史、每条的正文、修订号、状态、对应议题与板块，以及尚未汇总的已解决议题。用于在阶段完成后核对持久记录，而不必读整个项目。", "inputSchema": schema(PROJECT | {"stage_id": STR}, ["project_id"])},
]
READ_ONLY = {"relay_inbox", "relay_rules", "relay_project", "relay_task", "relay_issue", "relay_message", "relay_event", "relay_list", "relay_stages"}
for tool in TOOLS:
    tool['annotations']={'readOnlyHint':tool['name'] in READ_ONLY,'destructiveHint':False,'openWorldHint':False}
ACTIONS = {"relay_report": "report", "relay_raise_issue": "issue_create", "relay_send_message": "message", "relay_project_message": "project_message", "relay_review": "review", "relay_submit_summary": "summary_submit", "relay_request_summary": "summary_request", "relay_stage_summary": "summary_submit", "relay_follow_up_report": "report_submit"}
# Read-only tools map to their own bridge actions; none of them writes state
# except recording the member's last-seen timestamp, as relay_inbox already did.
READS = {"relay_rules": "rules", "relay_project": "read_project", "relay_task": "read_task",
         "relay_issue": "read_issue", "relay_message": "read_message", "relay_event": "read_event",
         "relay_stages": "read_stages",
         "relay_list": "read_list"}


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
        # This credential is scoped to the local panel. Connect directly so the
        # host application's proxy settings cannot redirect the bridge request.
        connection = HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request("POST", "/bridge", body=body, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            })
            response = connection.getresponse()
            status, raw = response.status, response.read()
        except (HTTPException, OSError):
            raise ValueError(f"无法连接本机 Stage Relay 面板（端口 {self.port}）。") from None
        finally:
            connection.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ValueError(f"本机面板返回了无法读取的响应（端口 {self.port}，HTTP {status}）。") from None
        if status != 200:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise ValueError(message or f"本机面板拒绝此操作（HTTP {status}）。")
        if not isinstance(payload, dict) or "result" not in payload:
            raise ValueError("本机面板的响应缺少协作结果，请核对面板与桥接版本。")
        return payload["result"]

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
                if name == "relay_rules":
                    result = {"version": rules.VERSION, "text": rules.full(self.agent)}
                elif name == "relay_inbox":
                    result = self.request("inbox", arguments)
                elif name in READS:
                    result = self.request(READS[name], arguments)
                elif name == "relay_follow_up_report" and arguments.get("status") == "failed":
                    # A failed generation is reported, not submitted with an empty
                    # body: the panel needs "failed, can retry" and the user must
                    # see why, so it goes through the failure path explicitly.
                    result = self.request("report_fail", arguments)
                else:
                    result = self.request(ACTIONS[name], arguments)
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
