import base64
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from relay.domain import Store
from relay.httpd import PanelServer


ROOT = Path(__file__).resolve().parent.parent


class Transport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-state-", dir=ROOT / "qa")
        assert Path(self.temp.name).resolve().is_relative_to((ROOT / "qa").resolve())
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.server = PanelServer(("127.0.0.1", 0), self.store, connection_home=Path(self.temp.name) / "fake-home")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        _, _, headers = self.http("GET", "/")
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.p = self.store.apply("project_create", {"title":"接口联调", "goal":"验证真实 HTTP 与 MCP stdio 的跨会话交接", "workspace":self.temp.name})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def http(self, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(value).encode() if value is not None else None
        connection.request(method,path,body,headers or {})
        response = connection.getresponse()
        status, data, headers = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return status, data, headers

    def user(self, action, **args):
        return self.http("POST","/api/action",{"action":action,"args":{"project_id":self.p["id"],**args}}, {"Cookie":self.cookie,"Content-Type":"application/json","X-Relay-Client":"panel"})

    def agent_http(self, action, args=None, agent="deepseek"):
        return self.http("POST","/bridge",{"action":action,"args":args or {}},{"Content-Type":"application/json","Authorization":"Bearer "+self.server.credentials[agent]})

    def mcp(self, agent, calls):
        requests = [
            {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"relay-test","version":"1"}}},
            {"jsonrpc":"2.0","method":"notifications/initialized"},
        ]
        requests.extend({"jsonrpc":"2.0","id":n+2,"method":"tools/call","params":{"name":name,"arguments":args}} for n,(name,args) in enumerate(calls))
        executable = os.environ.get("RELAY_TEST_BRIDGE_EXE")
        command = [executable] if executable else [sys.executable,str(ROOT / "bridge.py")]
        completed = subprocess.run(command+["--agent",agent,"--port",str(self.port),"--data-dir",self.temp.name], input="\n".join(json.dumps(r) for r in requests)+"\n", text=True,encoding="utf-8",capture_output=True,timeout=20,cwd=ROOT)
        self.assertEqual(completed.returncode,0,completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses),len(calls)+1)
        return responses[1:]

    def test_ui_api_requires_cookie_origin_and_header(self):
        self.assertEqual(self.http("GET","/api/state")[0],403)
        self.assertEqual(self.http("GET","/api/state",headers={"Cookie":self.cookie,"Origin":"https://evil.example"})[0],403)
        self.assertEqual(self.http("POST","/api/action",{"action":"pause","args":{}},{"Cookie":self.cookie,"Content-Type":"application/json"})[0],403)
        self.assertEqual(self.http("GET","/api/state",headers={"Cookie":self.cookie})[0],200)

    def test_bridge_cannot_resolve_or_impersonate(self):
        self.assertEqual(self.agent_http("resolve",{"actor":"user"})[0],403)
        self.assertEqual(self.http("POST","/bridge",{}, {"Content-Type":"application/json"})[0],403)
        self.assertEqual(self.http("GET","/api/state",headers={"Host":"evil.example","Cookie":self.cookie})[0],403)

    def test_rejected_post_returns_complete_forbidden_response(self):
        # An early close with unread request bytes can reset TCP on Windows,
        # hiding the intended 403 from the client instead of returning it.
        for _ in range(12):
            status,data,_=self.http('POST','/api/action',{'action':'pause','args':{},'padding':'x'*131072},{'Cookie':self.cookie,'Content-Type':'application/json'})
            self.assertEqual(status,403)
            self.assertIn('error',json.loads(data))

    def test_full_subprocess_mcp_handoff(self):
        self.user('assign',assignments={'planner':'codex','executor':'deepseek','reporter':'antigravity'})
        status,data,_ = self.user("task_create",title="实现接口",owner="deepseek",acceptance="测试命令与结果可核对")
        self.assertEqual(status,200)
        task_id=json.loads(data)["result"]["id"]
        self.assertEqual(self.user("dispatch")[0],200)
        report={"project_id":self.p["id"],"task_id":task_id,"status":"review","evidence":"测试客户端模拟：API 差异与测试结果已提交。"}
        replies=self.mcp("deepseek",[("relay_inbox",{}),("relay_report",report)])
        self.assertFalse(replies[-1]["result"]["isError"])
        inbox=json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertEqual(inbox["projects"][0]["tasks"][0]["status"],"queued")
        review={"project_id":self.p["id"],"task_id":task_id,"approved":True,"evidence":"测试客户端模拟：独立核对通过。"}
        reply=self.mcp("codex",[("relay_review",review)])[0]
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(self.user("summary_request")[0],200)
        reply=self.mcp("antigravity",[("relay_submit_summary",{"project_id":self.p["id"],"body":"测试客户端模拟：完整交接完成。"})])[0]
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(self.store.snapshot()["projects"][0]["summary"]["actor"],"antigravity")

    def test_stage_summary_tools_carry_the_scope_through_mcp(self):
        """`issue_id` and `stage_id` must reach the store, not be swallowed."""
        self.user("assign",assignments={"planner":"codex","executor":"deepseek",
                                       "reporter":"antigravity"})
        status,data,_=self.user("task_create",title="阶段板块",owner="deepseek",
                                acceptance="测试可核对")
        task_id=json.loads(data)["result"]["id"]
        self.user("dispatch")
        self.mcp("deepseek",[("relay_report",{"project_id":self.p["id"],"task_id":task_id,
                                             "status":"review","evidence":"提交检查结果"})])
        self.mcp("codex",[("relay_review",{"project_id":self.p["id"],"task_id":task_id,
                                          "approved":True,"evidence":"独立核对通过"})])
        status,data,_=self.user("issue_create",title="阶段一",body="问题",
                                members=["codex","deepseek"],
                                options=[{"title":"A","detail":"a"},{"title":"B","detail":"b"}])
        issue=json.loads(data)["result"]
        self.user("resolve",issue_id=issue["id"],choice=issue["options"][0]["id"])
        # The supervisor asks for one stage, not the whole project.
        asked=self.mcp("codex",[("relay_request_summary",{"project_id":self.p["id"],
                                                          "issue_id":issue["id"]})])[0]
        self.assertFalse(asked["result"]["isError"],asked)
        stage=json.loads(asked["result"]["content"][0]["text"])
        self.assertEqual(stage["issue_id"],issue["id"])
        self.assertEqual(stage["revision"],1)
        # The reporter answers against that stage id.
        replied=self.mcp("antigravity",[("relay_stage_summary",{
            "project_id":self.p["id"],"stage_id":stage["id"],"body":"阶段一完成"} )])[0]
        self.assertFalse(replied["result"]["isError"],replied)
        stages=self.store.snapshot()["projects"][0]["stages"]
        self.assertEqual([s["status"] for s in stages],["submitted"])

    def test_on_demand_reads_work_over_mcp(self):
        status, data, _ = self.user("task_create", title="按需读取", owner="deepseek",
                                    acceptance="证据可完整取回")
        self.assertEqual(status, 200)
        task_id = json.loads(data)["result"]["id"]
        self.assertEqual(self.user("dispatch")[0], 200)
        self.mcp("deepseek", [("relay_report", {"project_id": self.p["id"], "task_id": task_id,
                                                "status": "review",
                                                "evidence": "完整证据正文：" + "细节" * 300})])
        replies = self.mcp("deepseek", [
            ("relay_rules", {}),
            ("relay_project", {"project_id": self.p["id"]}),
            ("relay_task", {"project_id": self.p["id"], "task_id": task_id}),
            ("relay_message", {"project_id": self.p["id"], "message_id": self.message_id()}),
            ("relay_inbox", {"full": True}),
            ("relay_list", {"project_id": self.p["id"], "kind": "chat", "limit": 1}),
        ])
        self.assertFalse(replies[0]["result"]["isError"], replies[0])
        self.assertIn("身份与工具", json.loads(replies[0]["result"]["content"][0]["text"])["text"])
        project = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(project["tasks"][0]["id"], task_id)
        task = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertIn("完整证据正文", task["evidence"])
        message = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(message["body"], "跨会话读取单条消息")
        full = json.loads(replies[4]["result"]["content"][0]["text"])
        self.assertEqual(full["from_seq"], 0)
        page = json.loads(replies[5]["result"]["content"][0]["text"])
        self.assertEqual(page["returned"], 1)
        self.assertIn("next_offset", page)
        # Another member's project is refused rather than silently returned.
        other = self.mcp("antigravity", [("relay_task", {"project_id": self.p["id"], "task_id": task_id})])[0]
        self.assertTrue(other["result"]["isError"], other)

    def message_id(self):
        status, data, _ = self.user("project_message", body="跨会话读取单条消息")
        self.assertEqual(status, 200)
        return json.loads(data)["result"]["id"]

    def test_project_chat_across_panel_bridge_and_mcp(self):
        status,data,_=self.user("project_message",body="先在项目群聊里交接")
        self.assertEqual(status,200)
        self.assertEqual(json.loads(data)["result"]["actor"],"user")
        status,_,_=self.agent_http("project_message",{"project_id":self.p["id"],"body":"成员通过协作桥回复"})
        self.assertEqual(status,200)
        reply=self.mcp("deepseek",[("relay_project_message",{"project_id":self.p["id"],"body":"通过 MCP 工具回复"})])[0]
        self.assertFalse(reply["result"]["isError"],reply)
        chat=self.store.snapshot()["projects"][0]["chat"]
        self.assertEqual([m["body"] for m in chat],["先在项目群聊里交接","成员通过协作桥回复","通过 MCP 工具回复"])

    def test_close_and_cleanup_endpoints(self):
        headers={"Cookie":self.cookie,"Content-Type":"application/json","X-Relay-Client":"panel"}
        self.assertEqual(self.user("task_create",title="待执行板块",owner="deepseek",acceptance="不做也不能自行完成")[0],200)
        self.assertEqual(self.user("dispatch")[0],200)
        status,body,_=self.http("POST","/api/project/close",{"project_id":self.p["id"],"retention_days":2},headers)
        self.assertEqual(status,200)
        payload=json.loads(body)
        self.assertTrue(payload["result"]["closed"])
        self.assertEqual(payload["result"]["tasks"][0]["status"],"cancelled")
        self.assertIn("plan",payload)
        status,body,_=self.http("GET",f"/api/cleanup/preview?project={self.p['id']}",headers={"Cookie":self.cookie})
        self.assertEqual(status,200)
        preview=json.loads(body)
        self.assertIn("items",preview)
        self.assertEqual(self.http("POST","/api/cleanup/execute",{"project_id":self.p["id"]},headers)[0],400)
        self.assertEqual(self.http("POST","/api/cleanup/execute",{"project_id":self.p["id"],"confirm":True},headers)[0],400)
        status,body,_=self.http("POST","/api/cleanup/execute",{"project_id":self.p["id"],"plan_id":preview["id"],"confirm":True},headers)
        self.assertEqual(status,200)
        self.assertIn("moved",json.loads(body))
        self.assertEqual(self.user("dispatch")[0],400)

    def test_subprocess_tool_errors_remain_valid_json(self):
        replies=self.mcp("deepseek",[("relay_resolve",{}),("relay_report",{"actor":"user"})])
        self.assertTrue(all(r["result"]["isError"] for r in replies))

    def test_export_attachments_and_static_containment(self):
        upload={"name":"检查依据.txt","data":base64.b64encode(b"evidence").decode()}
        status,data,_=self.http("POST","/api/upload",upload,{"Cookie":self.cookie,"Content-Type":"application/json","X-Relay-Client":"panel"})
        self.assertEqual(status,200)
        file_id=json.loads(data)["id"]
        status,content,headers=self.http("GET",f"/files/{file_id}",headers={"Cookie":self.cookie})
        self.assertEqual((status,content),(200,b"evidence"))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
        self.assertEqual(self.http("GET","/../relay/domain.py")[0],404)
        status,body,_=self.http("GET",f"/api/export?project={self.p['id']}",headers={"Cookie":self.cookie})
        self.assertEqual(status,200)
        self.assertIn("接口联调",body.decode("utf-8"))

    def test_bad_input_does_not_stop_server(self):
        self.assertEqual(self.user("issue_create",title="x",body="x",options=[42])[0],400)
        self.assertEqual(self.http("GET","/api/health")[0],200)

    def test_test_environment_never_launches_real_apps(self):
        status,body,_=self.http("POST","/api/application/open",{"agent_id":"codex"},{"Cookie":self.cookie,"Content-Type":"application/json","X-Relay-Client":"panel"})
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)["opened"])

    def test_authorization_flow_and_revocation(self):
        headers={"Cookie":self.cookie,"Content-Type":"application/json","X-Relay-Client":"panel"}
        status,body,_=self.http("POST","/api/connection/prepare",{"agent_id":"deepseek"},headers)
        self.assertEqual(status,400)  # Absent DSH profile is detected without writing.
        status,body,_=self.http("POST","/api/connection/prepare",{"agent_id":"codex"},headers)
        self.assertEqual(status,200)
        plan=json.loads(body)
        self.assertEqual(self.http("POST","/api/connection/commit",{"plan_id":plan["plan_id"],"consent":False},headers)[0],400)
        status,body,_=self.http("POST","/api/connection/commit",{"plan_id":plan["plan_id"],"consent":True},headers)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)["configured"])
        agent=self.store.snapshot()["agents"][0]
        self.assertIsNone(agent["last_seen"])  # Configuration is not a native application response.
        self.assertEqual(self.agent_http("inbox",agent="codex")[0],200)
        status,body,_=self.http("POST","/api/connection/prepare",{"agent_id":"codex","remove":True},headers)
        self.assertEqual(status,200)
        self.http("POST","/api/connection/commit",{"plan_id":json.loads(body)["plan_id"],"consent":True},headers)
        self.assertEqual(self.agent_http("inbox",agent="codex")[0],403)


if __name__ == "__main__":
    unittest.main()
