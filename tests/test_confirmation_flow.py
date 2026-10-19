"""User confirmation and on-demand original-planner review, with fake models."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import test_relay as fixtures
from panel import Store, make_server
from relaylib.contracts import RelayError
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json
from relaylib.settings import Settings
from test_review import review


class ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RelayIntegrationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config['review'] = True
        self.config = self.f.base/'config.json'
        atomic_json(self.config, self.f.config)
        self.jobs = JobStore(self.config, self.f.base/'jobs', [self.f.project])
        self.addCleanup(lambda: [p.wait(timeout=20) for p in self.jobs.children])
        self.plan = {'goal':'修正数值','files':[{'path':'value.txt','note':'数值有误'}],
                     'steps':['写入 fixed'],'constraints':['保留测试']}

    def caller_job(self, key='confirmation'):
        atomic_json(self.f.sequence, {'cursor':0, 'entries':[{
            'response':fixtures.result(), 'write':{'value.txt':'fixed'}}]})
        request = '请修正数值并保留测试\n\n用户在确认时补充的要求：用中文报告'
        row = self.jobs.submit(str(self.f.project), key, self.plan,
                               user_request=request, review_mode='user_choice')
        return self.jobs.wait(row['job_id'],20)

    def test_original_task_reaches_worker_and_confirmation_persists_without_a_model(self):
        row = self.caller_job()
        self.assertEqual(row['status'],'completed',row)
        run = self.jobs.directory(row['job_id'])/'relay'
        prompt = (run/'001-worker/input.txt').read_text(encoding='utf-8')
        self.assertIn('请修正数值并保留测试',prompt)
        self.assertIn('用中文报告',prompt)
        original_state = (run/'state.json').read_bytes()
        confirmed = self.jobs.confirm_result(row['job_id'],row['review_revision'])
        self.assertEqual(confirmed['model_calls'],0)
        self.assertEqual(self.jobs.snapshot(row['job_id'])['status'],'user_confirmed')
        self.assertEqual((run/'state.json').read_bytes(),original_state)
        again = self.jobs.confirm_result(row['job_id'],row['review_revision'])
        self.assertEqual(confirmed['confirmation']['time'],again['confirmation']['time'])
        store = Store(self.jobs.root)
        self.assertEqual(store.runs()[0]['status'],'user_confirmed')
        self.assertEqual(store.snapshot(store.runs()[0]['id'])['confirmation']['source'],'user')
        self.assertEqual([r['body']['model'] for r in self.f.upstream.requests],['worker-model'])
        state = json.loads(original_state)
        state['last_result']['answer'] = 'changed answer'
        atomic_json(run/'state.json',state)
        self.assertTrue(self.jobs.snapshot(row['job_id'])['confirmation']['stale'])
        self.assertEqual(self.jobs.snapshot(row['job_id'])['status'],'completed')
        with self.assertRaises(RelayError):
            self.jobs.confirm_result(row['job_id'],row['review_revision'])

    def test_api_review_waits_for_user_choice_and_does_not_repeat_worker(self):
        atomic_json(self.f.sequence, {'cursor':0,'entries':self.f.good_entries()+[{'response':review()}]})
        submitted = self.jobs.submit_task(str(self.f.project),'api-choice','Fix value',review_mode='user_choice')
        row = self.jobs.wait(submitted['job_id'],20)
        self.assertEqual(row['status'],'completed',row)
        self.assertIsNone(row['review'])
        self.assertEqual(len(self.f.upstream.requests),2)
        self.jobs.request_review(row['job_id'],row['review_revision'])
        reviewed = self.jobs.wait(row['job_id'],20)
        self.assertEqual(reviewed['review']['reviewer'],'planner-model',reviewed)
        self.assertEqual([r['body']['model'] for r in self.f.upstream.requests],
                         ['planner-model','worker-model','planner-model'])
        self.assertTrue(self.jobs.request_review(row['job_id'],row['review_revision'])['reused'])
        self.assertEqual(len(self.f.upstream.requests),3)

    def test_user_confirmation_keeps_failed_test_evidence(self):
        self.f.config['limits']['max_fix_rounds'] = 0
        atomic_json(self.config,self.f.config)
        atomic_json(self.f.sequence,{'cursor':0,'entries':[{'response':fixtures.result()}]})
        submitted = self.jobs.submit(str(self.f.project),'accept-known-test-failure',self.plan,review_mode='user_choice')
        row = self.jobs.wait(submitted['job_id'],20)
        self.assertEqual(row['status'],'exhausted')
        self.assertEqual(row['checks'][0]['exit_code'],1)
        self.jobs.confirm_result(row['job_id'],row['review_revision'])
        final = self.jobs.snapshot(row['job_id'])
        self.assertEqual(final['status'],'user_confirmed')
        self.assertEqual(final['checks'][0]['exit_code'],1)

    def test_http_confirmation_is_same_origin_and_draft_polling_is_read_only(self):
        row = self.caller_job()
        server = make_server(Store(self.jobs.root),settings=Settings(self.config),jobs=self.jobs)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        draft = self.jobs.prepare(str(self.f.project),self.plan,user_request='新的任务')
        with urllib.request.urlopen(server.url+'api/draft?after='+draft['draft_id']) as response:
            unchanged=json.load(response)
        self.assertFalse(unchanged['changed']); self.assertNotIn('draft',unchanged)
        with urllib.request.urlopen(server.url+'api/draft?after=old') as response:
            self.assertEqual(json.load(response)['draft']['user_request'],'新的任务')
        body=json.dumps({'job_id':row['job_id'],'result_revision':row['review_revision']}).encode()
        def post(origin):
            request=urllib.request.Request(server.url+'api/confirm',data=body,headers={
                'Origin':origin,'Content-Type':'application/json','X-Relay-Panel':'settings'})
            return urllib.request.urlopen(request,timeout=10)
        with self.assertRaises(urllib.error.HTTPError) as error:
            post('http://invalid.example')
        self.assertEqual(error.exception.code,403); error.exception.close()
        with post(server.url.split('/',3)[0]+'//'+server.url.split('/',3)[2]) as response:
            self.assertEqual(json.load(response)['status'],'user_confirmed')
        self.assertEqual(len(self.f.upstream.requests),1)


if __name__=='__main__':
    unittest.main()
