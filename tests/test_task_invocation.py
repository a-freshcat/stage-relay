"""New invocations cannot inherit an executable stale OK task. No paid models."""
import json
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import test_relay as fixtures
from relaylib.contracts import RelayError
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json, require_clean_project


class TaskInvocationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = self.f.base / "config.json"
        atomic_json(self.config, self.f.config)
        self.jobs = JobStore(self.config, self.f.base / "jobs", [self.f.base])
        self.plan = {"goal": "回答 OK", "files": [{"path": "value.txt", "note": "Existing anchor"}], "steps": ["Reply OK"]}
        self.jobs.prepare(self.f.project, self.plan, user_request="回答OK")
        self.panel = patch("relaylib.panel_runtime.open_panel", return_value={"url": "http://localhost/panel/", "model_calls": 0})
        self.panel.start(); self.addCleanup(self.panel.stop)

    def begin(self, request="生成鹈鹕骑自行车的 SVG"):
        return self.jobs.open_panel(user_request=request)["task"]

    def artifact_plan(self):
        return {"goal": "生成鹈鹕骑自行车的 SVG", "files": [{"path": "README.md", "note": "本次任务说明"}],
                "steps": ["创建 pelican-bicycle.svg，包含鹈鹕与自行车", "检查 SVG 有效且不引用外部素材"]}

    def test_task_is_visible_before_plan_and_standalone_project_is_clean(self):
        task = self.begin()
        draft = self.jobs.draft()
        self.assertEqual(draft["status"], "preparing")
        self.assertIsNone(draft["plan"])
        self.assertEqual(draft["user_request"], "生成鹈鹕骑自行车的 SVG")
        project = Path(task["project"])
        require_clean_project(project)
        self.assertTrue((project / "README.md").is_file())
        self.assertFalse((project / "pelican-bicycle.svg").exists())
        self.assertFalse(self.jobs.root.is_relative_to(project))
        self.assertEqual(self.f.upstream.requests, [])

    def test_prepare_ready_is_bound_to_invocation_and_old_start_is_rejected(self):
        task = self.begin()
        result = self.jobs.prepare(task["project"], self.artifact_plan(), user_request=task["user_request"], invocation_id=task["invocation_id"])
        self.assertEqual(self.jobs.draft()["status"], "ready")
        self.assertEqual(self.jobs.draft()["invocation_id"], task["invocation_id"])
        with patch.object(self.jobs, "launch") as launch:
            with self.assertRaises(RelayError) as caught:
                self.jobs.submit(task["project"], "old", self.artifact_plan(), draft_id=task["id"])
            self.assertEqual(caught.exception.code, "draft_stale")
            self.jobs.submit(task["project"], "current", self.artifact_plan(), draft_id=result["draft_id"])
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.f.upstream.requests, [])

    def test_failed_legacy_prepare_replaces_ok_with_new_task_and_visible_reason(self):
        with self.assertRaises(RelayError) as caught:
            self.jobs.prepare(self.f.base, self.artifact_plan(), user_request="生成鹈鹕骑自行车的 SVG")
        self.assertEqual(caught.exception.code, "unsafe_path")
        draft = self.jobs.draft()
        self.assertEqual(draft["status"], "blocked")
        self.assertEqual(draft["user_request"], "生成鹈鹕骑自行车的 SVG")
        self.assertIsNone(draft["plan"])
        self.assertEqual(draft["error"]["code"], "unsafe_path")
        self.assertIn("独立", draft["error"]["action"])

    def test_missing_future_output_does_not_require_implementing_it_before_handoff(self):
        task = self.begin()
        plan = self.artifact_plan(); plan["files"][0]["path"] = "not-created.svg"
        with self.assertRaises(RelayError):
            self.jobs.prepare(task["project"], plan, user_request=task["user_request"], invocation_id=task["invocation_id"])
        draft = self.jobs.draft()
        self.assertEqual(draft["status"], "blocked")
        self.assertIn("实施步骤", draft["error"]["action"])
        self.jobs.prepare(task["project"], self.artifact_plan(), user_request=task["user_request"], invocation_id=task["invocation_id"])
        self.assertEqual(self.jobs.draft()["status"], "ready")
        self.assertFalse((Path(task["project"]) / "pelican-bicycle.svg").exists())

    def test_late_prepare_cannot_replace_a_newer_invocation(self):
        old = self.begin("旧任务")
        new = self.begin("新任务")
        with self.assertRaises(RelayError) as caught:
            self.jobs.prepare(old["project"], self.artifact_plan(), user_request="旧任务", invocation_id=old["invocation_id"])
        self.assertEqual(caught.exception.code, "draft_stale")
        self.assertEqual(self.jobs.draft()["invocation_id"], new["invocation_id"])
        self.assertEqual(self.jobs.draft()["user_request"], "新任务")

    def test_bare_open_does_not_replace_task(self):
        before = self.jobs.draft()
        self.jobs.open_panel()
        self.assertEqual(self.jobs.draft(), before)

    def test_old_browser_without_draft_identity_cannot_start_ok_after_new_invocation(self):
        from panel import Store, make_server
        from relaylib.settings import Settings
        self.begin()
        server = make_server(Store(self.jobs.root), settings=Settings(self.config), jobs=self.jobs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        url = urlsplit(server.url)
        body = {"project": str(self.f.project), "task_key": "stale-tab", "plan": self.plan, "user_request": "回答OK"}
        request = urllib.request.Request(server.url + "api/jobs", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Relay-Panel": "settings", "Origin": url.scheme + "://" + url.netloc})
        with patch.object(self.jobs, "launch") as launch:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(json.load(caught.exception)["code"], "draft_stale")
            caught.exception.close()
            launch.assert_not_called()
        self.assertEqual(self.f.upstream.requests, [])

    def test_actual_ui_replaces_ok_with_preparing_error_and_ready_states(self):
        from relaylib.runner import ROOT
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const compose=require(process.argv[2]),source=fs.readFileSync(process.argv[1],'utf8');
function node(tag,text,cls){return {tag,text:text||'',cls,children:[],dataset:{},value:'',append(...v){this.children.push(...v)},replaceChildren(){this.children=[]}}}
const els={};const $=id=>els[id]||=node('div');$('launch-mode').value='caller';
const pending={id:'new',invocation_id:'new',status:'preparing',user_request:'生成鹈鹕骑自行车的SVG',project:'p2',plan:null};
const ctx={node,$,RelayCompose:compose,callerDraft:null,launchEnabled:true,pendingJob:'',repeatReady:false,loadedDraftId:'',draftLoading:false,lastDraftCheck:0,loadedDraftGoal:'',dismissedDraftId:'',readable(){},detail(){return node('details')},Date,api:async()=>ctx.next};
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function callerReady('),source.indexOf('async function launchOptions(')),ctx);
(async()=>{
 ctx.next={draft:{id:'old',status:'ready',user_request:'回答OK',project:'p1',plan:{goal:'回答OK',steps:[]}}};await ctx.loadDraft();
 assert.equal($('launch-request').value,'回答OK');$('launch-extra').value='旧任务的补充';
 ctx.next={draft:pending};await ctx.loadDraft(true);
 assert.equal($('launch-request').value,pending.user_request);assert.equal($('launch-extra').value,'');assert.equal($('launch-start').disabled,true);
 assert.equal($('launch-start').textContent,'等待 Codex 方案');assert.equal($('caller-copy').hidden,false);
 $('launch-extra').value='新任务的补充';
 ctx.next={draft:{...pending,id:'blocked',status:'blocked',error:{message:'工作目录不正确',action:'使用独立工作目录',code:'unsafe_path'}}};await ctx.loadDraft(true);
 assert.equal($('launch-start').disabled,true);assert.ok(JSON.stringify($('draft-preview')).includes('工作目录不正确'));
 ctx.next={draft:{...pending,id:'ready',status:'ready',plan:{goal:pending.user_request,steps:['生成SVG']}}};await ctx.loadDraft(true);
 assert.equal($('launch-start').disabled,false);assert.equal($('launch-start').textContent,'开始任务');assert.equal($('caller-copy').hidden,true);assert.equal($('launch-extra').value,'新任务的补充');
 $('launch-extra').value='';ctx.launchMode();assert.equal($('launch-start').disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which("node") or "node", "-e", script, str(ROOT / "panel/panel.js"), str(ROOT / "panel/compose.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_waiting_plan_is_polled_without_the_idle_history_delay(self):
        from relaylib.runner import ROOT
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8'),els={};
const $=id=>els[id]||={value:'',replaceChildren(){},append(){}};$('launch-mode').value='caller';
const ctx={$,Date,node(){return {}},document:{hidden:false},launchEnabled:true,pendingJob:'',currentActive:false,
 callerDraft:{status:'preparing',plan:null},busy:false,lastAutomaticRefresh:Date.now(),lastDraftCheck:0,
 current:'',listTick:0,lastDeleted:'',lastDeletedIds:[],runsKey:'',snapshotKey:'',polls:0,
 loadDraft:async()=>{ctx.polls++;ctx.callerDraft={status:'ready',plan:{goal:'新方案'}}},
 api:async()=>({runs:[]}),renderHistoryChoices(){},renderFailure(){}};
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('async function refresh('),source.indexOf('$("run").addEventListener')),ctx);
(async()=>{await ctx.refresh();assert.equal(ctx.polls,1);assert.equal(ctx.callerDraft.status,'ready')})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([shutil.which("node") or "node", "-e", script, str(ROOT / "panel/panel.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
