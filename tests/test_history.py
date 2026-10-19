"""History removal preserves checkpoints and idempotency; local fixtures only."""
import json
import time
import unittest
import threading
import urllib.request
import urllib.error
from unittest.mock import patch
import test_relay as fixtures
from panel import Store, make_server
from relaylib.plugin import JobStore
from relaylib.runtime import atomic_json, FileLock
from relaylib.contracts import RelayError


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.RelayIntegrationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        config=self.f.base/'config.json';atomic_json(config,self.f.config)
        self.jobs=JobStore(config,self.f.base/'jobs',[self.f.project])
        with patch.object(self.jobs,'launch'):
            row=self.jobs.submit_task(str(self.f.project),'history-case','保留项目，删除历史记录')
        self.job_id=row['job_id'];self.job=self.jobs.directory(self.job_id)
        atomic_json(self.job/'service.json',{'status':'stopped','error':'endpoint_unavailable','time':time.time()})
        self.store=Store(self.jobs.root);self.run_id=Store.identity(self.job/'relay')

    def test_remove_restore_and_duplicate_submission_keep_original_checkpoint(self):
        before=(self.job/'job.json').read_bytes()
        self.store.change_history(self.run_id,'delete')
        self.assertEqual(self.store.runs(),[])
        self.assertIsNone(self.store.snapshot(self.run_id))
        self.assertEqual((self.job/'job.json').read_bytes(),before)
        with patch.object(self.jobs,'launch') as launch:
            row=self.jobs.submit_task(str(self.f.project),'history-case','保留项目，删除历史记录')
        self.assertTrue(row['reused']);launch.assert_not_called()
        Store(self.jobs.root).change_history(self.run_id,'restore')
        self.assertEqual(self.store.runs()[0]['id'],self.run_id)
        self.assertEqual(self.f.upstream.requests,[])

    def test_running_queued_and_locked_jobs_cannot_be_removed(self):
        for status in ('running','queued'):
            atomic_json(self.job/'service.json',{'status':status,'time':time.time()})
            with self.assertRaises(RelayError) as error:self.store.change_history(self.run_id,'delete')
            self.assertEqual(error.exception.code,'busy')
        atomic_json(self.job/'service.json',{'status':'stopped','time':0})
        with FileLock(self.job/'service.lock'):
            with self.assertRaises(RelayError):self.store.change_history(self.run_id,'delete')
        self.assertEqual(len(self.store.runs()),1)

    def test_unknown_id_or_path_cannot_select_a_directory(self):
        for value in ('../project', 'a'*24):
            with self.assertRaises(RelayError):self.store.change_history(value,'delete')
        self.assertTrue(self.f.project.is_dir())

    def test_http_delete_restore_require_same_origin_and_preserve_job(self):
        from relaylib.settings import Settings
        server=make_server(self.store,settings=Settings(self.jobs.config),jobs=self.jobs)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def post(action,origin):
            data=json.dumps({'id':self.run_id,'action':action}).encode()
            req=urllib.request.Request(server.url+'api/history',data=data,headers={
                'Content-Type':'application/json','X-Relay-Panel':'settings','Origin':origin})
            with urllib.request.urlopen(req) as response:return json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as error:post('delete','http://untrusted.invalid')
        self.assertEqual(error.exception.code,403);error.exception.close()
        origin='http://127.0.0.1:'+str(server.server_port)
        self.assertEqual(post('delete',origin)['generation_requests'],0)
        self.assertEqual(self.store.runs(),[])
        with urllib.request.urlopen(server.url+'api/runs') as response:
            self.assertEqual(json.load(response)['last_deleted'],self.run_id)
        post('restore',origin)
        self.assertEqual(self.store.runs()[0]['id'],self.run_id)
        self.assertTrue((self.job/'job.json').is_file())
