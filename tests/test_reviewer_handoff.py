import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from relay.codex import CodexHost, CodexManager, NativeError
from relay.domain import Store, Invalid
from relay.runtime import Runtime

ROOT=Path(__file__).resolve().parent.parent


class ReviewerHandoff(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='test-state-',dir=ROOT/'qa')
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'state.sqlite3')
        self.p=self.store.apply('project_create',{'title':'审核交接','workspace':self.temp.name})
        self.pid=self.p['id']
        self.task=self.store.apply('task_create',{'project_id':self.pid,'title':'成果','owner':'deepseek','acceptance':'独立核验'})
        self.store.apply('dispatch',{'project_id':self.pid})
        self.store.apply('report',{'project_id':self.pid,'task_id':self.task['id'],'status':'review','evidence':'原成果'},actor='deepseek')

    def assign(self,planner):
        return self.store.apply('assign',{'project_id':self.pid,'assignments':{'planner':planner,'executor':'deepseek','reporter':'codex'}})

    def review(self,actor,epoch=None):
        args={'project_id':self.pid,'task_id':self.task['id'],'approved':True,'evidence':'独立检查'}
        if epoch is not None:args['review_epoch']=epoch
        return self.store.apply('review',args,actor=actor)

    def test_switch_preserves_evidence_and_late_review_is_reference(self):
        self.assign('antigravity')
        result=self.review('codex',0)
        self.assertTrue(result['reference_only'])
        p=self.store.snapshot()['projects'][0]
        self.assertEqual(p['tasks'][0]['status'],'review')
        self.assertEqual(p['tasks'][0]['evidence'],'原成果')
        self.assertEqual(len(p['tasks'][0]['review_history']),1)
        self.review('antigravity',1)
        self.assertEqual(self.store.snapshot()['projects'][0]['tasks'][0]['status'],'done')

    def test_switch_back_cannot_accept_a_result_from_old_round(self):
        self.assign('antigravity');self.assign('codex')
        self.assertTrue(self.review('codex',0)['reference_only'])
        with self.assertRaises(Invalid):self.review('codex')
        self.review('codex',2)

    def test_release_frees_only_this_panels_connection(self):
        """Releasing must not delete the authorization, thread or shared app."""
        from relay.runtime import Runtime
        Runtime(self.store)
        self.store.apply('setup_record',{'agent_id':'codex','configured':True,'path':'fixture'})
        entry={'command':'fixture'}
        server=SimpleNamespace(store=self.store,connections=SimpleNamespace(owned={'codex':{'entry':entry}},entry=lambda _:entry))
        manager=CodexManager(server)
        closed=[]
        class Host:
            def __init__(self,*args):pass
            def snapshot(self):return {'agent':'codex','connected':True,'approvals':[]}
            def retry_sessions(self):pass
            def close(self):closed.append(1)
        with patch('relay.codex.CodexHost',Host):
            manager.request_connect('codex')
            for _ in range(100):
                if manager.snapshot() and manager.snapshot()[0].get('connectionStatus')=='connected':
                    break
                time.sleep(.01)
            result=manager.release('codex')
            self.assertTrue(result['released'])
            self.assertEqual(closed,[1])
            self.assertEqual(manager.snapshot(),[])
        # The app authorization survives, so reconnecting needs no re-setup.
        member=next(a for a in self.store.snapshot()['agents'] if a['id']=='codex')
        self.assertTrue(member['setup']['configured'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_enabled").fetchone()[0],0)
            self.assertTrue(db.execute("SELECT COUNT(*) FROM codex_threads").fetchone()[0] >= 0)

    def test_background_connect_does_not_block_state_or_duplicate_hosts(self):
        from relay.runtime import Runtime
        Runtime(self.store)
        self.store.apply('setup_record',{'agent_id':'codex','configured':True,'path':'fixture'})
        entry={'command':'fixture'}
        server=SimpleNamespace(store=self.store,connections=SimpleNamespace(owned={'codex':{'entry':entry}},entry=lambda _:entry))
        manager=CodexManager(server)
        entered=threading.Event();release=threading.Event();constructed=[]
        class SlowHost:
            def __init__(self,*args):
                constructed.append(1);entered.set();release.wait(3)
            def snapshot(self):return {'agent':'codex','connected':True,'approvals':[]}
            def retry_sessions(self):pass
            def close(self):pass
        with patch('relay.codex.CodexHost',SlowHost):
            manager.request_connect('codex');self.assertTrue(entered.wait(1))
            try:
                manager.request_connect('codex')
                before=time.monotonic();status=manager.snapshot()
                self.assertLess(time.monotonic()-before,.5)
                self.assertEqual(status[0]['connectionStatus'],'connecting')
                self.assertEqual(len(constructed),1)
            finally:release.set()
            for _ in range(100):
                if manager.snapshot()[0]['connectionStatus']!='connecting':break
                time.sleep(.01)
            manager.connect('codex')
            self.assertEqual(len(constructed),1)
        manager.close()

    def test_occupied_thread_does_not_prevent_other_threads_from_resuming(self):
        host=CodexHost.__new__(CodexHost)
        host.lock=threading.RLock();host.unavailable={};remembered=[]
        def request(method,args,**kwargs):
            if args['threadId']=='busy':raise NativeError('thread busy already has an active writer')
            return {'thread':{'id':'ready'}}
        host.rpc=SimpleNamespace(request=request);host._remember=remembered.append
        host._resume('busy',self.temp.name);host._resume('ready',self.temp.name)
        self.assertIn('active writer',host.unavailable['busy'])
        self.assertEqual(len(remembered),1)

    def test_model_selection_uses_existing_writer_and_next_turn(self):
        calls=[];receipts=[]
        def request(method,args):
            calls.append((method,args))
            return {'data':[{'model':'model-b'}]} if method=='model/list' else {'turn':{'id':'turn-next'}}
        host=CodexHost.__new__(CodexHost)
        host.rpc=SimpleNamespace(request=request);host.lock=threading.RLock()
        host.sessions={'session':{'provider':'provider','model':'model-a'}};host.active={}
        host.server=SimpleNamespace(runtime=SimpleNamespace(receipt=lambda *args:receipts.append(args)))
        host.agent='codex'
        host._job({'id':'model','session':'session','kind':'model-select','selection':{'provider':'provider','model':'model-b'}})
        host._job({'id':'review','session':'session','kind':'report','prompt':'检查原有成果'})
        self.assertEqual([m for m,_ in calls],['model/list','turn/start'])
        self.assertEqual(calls[-1][1]['model'],'model-b')

    def test_codex_model_catalog_exposes_advertised_efforts(self):
        efforts=[{'reasoningEffort':'low','description':'fast'},
                 {'reasoningEffort':'high','description':'deep'}]
        def request(method,args):
            if method=='model/list':
                return {'data':[{'model':'with-effort','displayName':'With Effort',
                                 'supportedReasoningEfforts':efforts,
                                 'defaultReasoningEffort':'low'},
                                {'model':'plain','displayName':'Plain'}]}
            return {'turn':{'id':'t'}}
        receipts=[]
        host=CodexHost.__new__(CodexHost)
        host.rpc=SimpleNamespace(request=request);host.lock=threading.RLock()
        host.sessions={'session':{'provider':'provider','model':'model-a'}};host.active={}
        host.server=SimpleNamespace(runtime=SimpleNamespace(receipt=lambda *a:receipts.append(a)))
        host.agent='codex'
        host._job({'id':'cat','session':'session','kind':'model-catalog'})
        models=receipts[0][1]['result']['models']
        rich=[m for m in models if m['model']=='with-effort'][0]
        flat=[m for m in models if m['model']=='plain'][0]
        # Only an advertising model offers depths; the other offers none.
        self.assertEqual(rich['efforts'],['low','high'])
        self.assertEqual(rich['effortDefault'],'low')
        self.assertNotIn('efforts',flat)

    def test_codex_effort_is_validated_and_sent_on_the_turn(self):
        def request(method,args):
            if method=='model/list':
                return {'data':[{'model':'m','supportedReasoningEfforts':
                                 [{'reasoningEffort':'low'},{'reasoningEffort':'max'}]}]}
            return {'turn':{'id':'turn-next'}}
        receipts=[]
        host=CodexHost.__new__(CodexHost)
        host.rpc=SimpleNamespace(request=request);host.lock=threading.RLock()
        host.sessions={'session':{'provider':'provider','model':'m'}};host.active={}
        host.server=SimpleNamespace(runtime=SimpleNamespace(receipt=lambda *a:receipts.append(a)))
        host.agent='codex'
        host._job({'id':'sel','session':'session','kind':'model-select',
                   'selection':{'provider':'provider','model':'m','reasoningEffort':'max'}})
        self.assertEqual(receipts[-1][1]['status'],'delivered')
        seen={}
        host.rpc=SimpleNamespace(request=lambda m,a: (seen.update(a),{'turn':{'id':'t'}})[1])
        host._job({'id':'run','session':'session','kind':'report','prompt':'go'})
        self.assertEqual(seen['effort'],'max')
        # An effort the model never advertised is refused, because the server
        # accepts arbitrary strings and would not catch it for us. The loop turns
        # this NativeError into a failed receipt, so it must raise here.
        host.rpc=SimpleNamespace(request=request)
        with self.assertRaises(NativeError) as raised:
            host._job({'id':'bad','session':'session','kind':'model-select',
                       'selection':{'provider':'provider','model':'m','reasoningEffort':'ultra'}})
        self.assertIn('思考深度',str(raised.exception))

    def test_old_reviewer_queued_work_is_cancelled_without_touching_result(self):
        runtime=Runtime(self.store)
        runtime.poll('codex',{'sessions':[{'id':'native','workspace':self.temp.name}]})
        runtime.bind(self.pid,'codex','native')
        with self.store.connect() as db:
            db.execute('INSERT INTO native_jobs VALUES(?,?,?,?,?,?,?,?)',('old-review',self.pid,'codex','native',None,'queued','{"kind":"report","prompt":"old"}',time.time()))
            event=db.execute("SELECT seq FROM events WHERE project=? AND kind='task_queued'",(self.pid,)).fetchone()[0]
            db.execute('INSERT INTO native_jobs VALUES(?,?,?,?,?,?,?,?)',('old-supervision',self.pid,'codex','native',event,'queued','{"kind":"task_queued","prompt":"old"}',time.time()))
        self.assign('antigravity')
        delivered=runtime.poll('codex',{'sessions':[{'id':'native','workspace':self.temp.name}]})
        self.assertNotIn('old-review',[j['id'] for j in delivered['jobs']])
        self.assertNotIn('old-supervision',[j['id'] for j in delivered['jobs']])
        self.assertEqual(next(j for j in runtime.snapshot()['jobs'] if j['id']=='old-review')['status'],'cancelled')
        self.assertEqual(self.store.snapshot()['projects'][0]['tasks'][0]['status'],'review')

    def test_independent_session_can_be_prepared_while_paused_without_replaying_work(self):
        runtime=Runtime(self.store)
        runtime.poll('codex',{'sessions':[{'id':'occupied','workspace':self.temp.name}]})
        runtime.bind(self.pid,'codex','occupied')
        self.store.apply('pause',{'project_id':self.pid,'paused':True})
        with self.assertRaises(Invalid):runtime.create_session(self.pid,'codex')
        job=runtime.create_session(self.pid,'codex',replace=True)
        with self.assertRaises(Invalid):runtime.create_session(self.pid,'codex',replace=True)
        delivered=runtime.poll('codex',{'sessions':[]})['jobs']
        self.assertEqual([j['id'] for j in delivered],[job['id']])
        runtime.created('codex',{'id':job['id'],'session':'independent'})
        self.assertEqual(runtime.poll('codex',{'sessions':[{'id':'independent','workspace':self.temp.name}]})['jobs'],[])
        self.assertEqual(runtime.snapshot()['bindings'][0]['session'],'independent')
        old=next(r for r in runtime.snapshot()['resources'] if r['session']=='occupied')
        self.assertEqual(old['status'],'unbound')
        p=self.store.snapshot()['projects'][0]
        self.assertTrue(p['paused']);self.assertEqual(p['tasks'][0]['evidence'],'原成果')

    def test_delayed_session_creation_does_not_override_newer_binding(self):
        runtime=Runtime(self.store)
        sessions=[{'id':x,'workspace':self.temp.name} for x in ['old','user-choice']]
        runtime.poll('codex',{'sessions':sessions});runtime.bind(self.pid,'codex','old')
        job=runtime.create_session(self.pid,'codex',replace=True)
        for pending in runtime.poll('codex',{'sessions':sessions})['jobs']:
            if pending.get('kind')=='create-session':
                continue
            runtime.receipt('codex',{'id':pending['id'],'status':'delivered','detail':'fixture'})
        # Execution in-flight is what blocks a switch; the create request itself
        # is control traffic. The late result is then refused, not applied.
        runtime.bind(self.pid,'codex','user-choice')
        result=runtime.created('codex',{'id':job['id'],'session':'late-created'})
        self.assertFalse(result['bound'])
        self.assertEqual(runtime.snapshot()['bindings'][0]['session'],'user-choice')

    def test_new_thread_is_persisted_before_binding(self):
        calls=[]
        host=CodexHost.__new__(CodexHost);host.agent='codex'
        def request(method,args):
            calls.append(method)
            return {'thread':{'id':'durable'}} if method=='thread/start' else {}
        host.rpc=SimpleNamespace(request=request)
        host._remember=lambda result:{'id':result['thread']['id'],'model':'fixture','provider':'fixture'}
        host.server=SimpleNamespace(runtime=SimpleNamespace(created=lambda *args:calls.append('bind')))
        host._event=lambda *args:None
        host._job({'id':'create','kind':'create-session','workspace':self.temp.name})
        self.assertEqual(calls,['thread/start','thread/name/set','bind'])

    def test_close_releases_only_threads_this_host_actually_owns(self):
        calls=[];host=CodexHost.__new__(CodexHost)
        host.stopped=threading.Event();host.active={};host.sessions={'owned':{}}
        host.unavailable={'not-owned':'active writer'}
        host.thread=SimpleNamespace(join=lambda **kwargs:None)
        host.rpc=SimpleNamespace(request=lambda method,args,**kwargs:calls.append((method,args)),close=lambda:calls.append('closed'))
        host.close()
        self.assertEqual(calls,[('thread/unsubscribe',{'threadId':'owned'}),'closed'])
