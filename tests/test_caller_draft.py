"""Current conversation planning is a local draft; execution skips the planner."""
import io
import json
import unittest
import urllib.request
import threading
from pathlib import Path
from unittest.mock import patch
import test_relay as fixtures
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json
from relaylib.contracts import RelayError
from mcp_server import serve
from panel import Store,make_server
from relaylib.settings import Settings


class CallerDraftTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RelayIntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        config=self.f.base/'config.json';atomic_json(config,self.f.config)
        self.jobs=JobStore(config,self.f.base/'jobs',[self.f.project])
        self.plan={'goal':'修正数值','files':[{'path':'value.txt','note':'当前数值为 wrong'}],'steps':['将数值修改为 fixed 并验证']}

    def test_prepare_is_local_and_panel_reads_the_exact_plan(self):
        with patch('relaylib.gateway.resolve_endpoint',side_effect=AssertionError('No endpoint resolution')):
            first=self.jobs.prepare(str(self.f.project),self.plan)
            second=self.jobs.prepare(str(self.f.project),self.plan)
        self.assertEqual(first['draft_id'],second['draft_id'])
        self.assertEqual(first['model_calls'],0)
        self.assertEqual(self.jobs.draft()['plan'],self.plan)
        self.assertEqual(self.f.upstream.requests,[])
        self.assertEqual(Store(self.jobs.root).runs(),[])
        self.assertEqual((self.f.project/'value.txt').read_text(),'wrong')

    def test_prepared_draft_executes_only_worker(self):
        self.jobs.prepare(str(self.f.project),self.plan)
        atomic_json(self.f.sequence,{'cursor':0,'entries':[{'response':fixtures.result(),'write':{'value.txt':'fixed'}}]})
        row=self.jobs.submit(str(self.f.project),'draft-worker',self.jobs.draft()['plan'])
        result=self.jobs.wait(row['job_id'],20)
        for child in self.jobs.children:child.wait(timeout=5)
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual([r['body']['model'] for r in self.f.upstream.requests],['worker-model'])
        state=json.loads((self.jobs.directory(row['job_id'])/'relay/state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['plan_source'],'caller')
        self.assertEqual(len(state['attempts']),1)

    def test_original_user_instructions_are_local_and_kept_in_worker_plan(self):
        original = json.loads(json.dumps(self.plan))
        self.jobs.prepare(str(self.f.project), self.plan,
                          user_request='请修正数值，并保留已有测试', instructions=['保留已有测试'])
        draft = self.jobs.draft()
        self.assertEqual(draft['user_request'], '请修正数值，并保留已有测试')
        self.assertEqual(draft['instructions'], ['保留已有测试'])
        self.assertIn('保留已有测试', draft['plan']['constraints'])
        self.assertEqual(self.plan, original)
        self.assertEqual(self.f.upstream.requests, [])

    def test_mcp_prepare_does_not_submit_or_start_a_job(self):
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'relay_prepare','arguments':{'project':str(self.f.project),'plan':self.plan}}}]
        reader=io.BytesIO(('\n'.join(json.dumps(m) for m in messages)+'\n').encode());writer=io.BytesIO()
        serve(self.jobs,reader,writer)
        result=json.loads(writer.getvalue().splitlines()[-1])['result']
        self.assertNotIn('isError',result)
        self.assertEqual(self.f.upstream.requests,[])
        self.assertIsNotNone(self.jobs.draft())

    def test_panel_default_and_draft_endpoint(self):
        self.jobs.prepare(str(self.f.project),self.plan)
        server=make_server(Store(self.jobs.root),settings=Settings(self.jobs.config),jobs=self.jobs)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        with urllib.request.urlopen(server.url+'api/draft') as response:
            self.assertEqual(json.load(response)['draft']['plan'],self.plan)
        with urllib.request.urlopen(server.url) as response:
            html=response.read().decode()
            self.assertLess(html.index('value="caller"'),html.index('value="serial"'))
        self.assertEqual(self.f.upstream.requests,[])
