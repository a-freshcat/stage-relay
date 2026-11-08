import tempfile
import unittest
import time
from unittest.mock import patch
from pathlib import Path

from relay.domain import Store, Invalid
from relay.runtime import Runtime


class NativeRuntime(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parent.parent/'qa'
        self.temp=tempfile.TemporaryDirectory(prefix='test-state-',dir=root)
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'state.sqlite3')
        self.runtime=Runtime(self.store)
        self.project=self.store.apply('project_create',{'title':'原生交接','goal':'由原应用使用工具完成工作','workspace':self.temp.name})
        self.sessions=[{'id':'native-one','workspace':self.temp.name,'status':'idle','model':'native-model'}]
        self.runtime.poll('deepseek',{'app':'dsh','sessions':self.sessions})

    def test_binding_requires_same_workspace(self):
        self.runtime.poll('deepseek',{'app':'dsh','sessions':[{'id':'wrong','workspace':str(Path(self.temp.name).parent)}]})
        with self.assertRaises(Invalid):
            self.runtime.bind(self.project['id'],'deepseek','wrong')

    def test_real_queue_requires_receipt_and_is_not_replayed(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('task_create',{'project_id':pid,'title':'执行检查','acceptance':'保存实际工具结果','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        first=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs']
        self.assertEqual(len(first),1)
        self.assertIn('原应用的工具',first[0]['prompt'])
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'],[])
        self.runtime.receipt('deepseek',{'id':first[0]['id'],'status':'delivered'})
        self.assertEqual(self.runtime.snapshot()['jobs'][0]['status'],'delivered')
        self.assertEqual(self.store.snapshot()['projects'][0]['tasks'][0]['status'],'queued')

    def test_pause_prevents_delivery_and_unknown_session_cannot_report(self):
        self.runtime.bind(self.project['id'],'deepseek','native-one')
        self.store.apply('pause',{'project_id':self.project['id'],'paused':True})
        response=self.runtime.poll('deepseek',{'sessions':self.sessions})
        self.assertEqual(response['jobs'],[])
        self.assertEqual(response['paused_sessions'],['native-one'])
        with self.assertRaises(Invalid):
            self.runtime.observe('deepseek',{'session':'unbound','seq':1,'event':{'type':'tool/call'}})

    def test_create_session_requires_user_job_and_preserves_native_identity(self):
        with self.assertRaises(Invalid):
            self.runtime.created('deepseek',{'id':'fake','session':'pretend'})
        request=self.runtime.create_session(self.project['id'],'deepseek')
        job=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'][0]
        self.assertEqual(job['kind'],'create-session')
        self.runtime.created('deepseek',{'id':request['id'],'session':'native-created'})
        self.assertEqual(self.runtime.snapshot()['bindings'][0]['session'],'native-created')

    def test_tool_activity_is_attributed_and_deduplicated(self):
        self.runtime.bind(self.project['id'],'deepseek','native-one')
        event={'session':'native-one','seq':2,'event':{'type':'tool/call','data':{'name':'subagent_create'}}}
        self.runtime.observe('deepseek',event)
        self.runtime.observe('deepseek',event)
        activity=self.runtime.snapshot()['activity']
        self.assertEqual(len(activity),1)
        self.assertEqual(activity[0]['data']['name'],'subagent_create')

    def test_execution_review_wakes_native_supervisor(self):
        pid=self.project['id']
        self.runtime.poll('codex',{'sessions':self.sessions})
        self.runtime.bind(pid,'codex','native-one')
        task=self.store.apply('task_create',{'project_id':pid,'title':'检查文件','acceptance':'读回文件确认','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        initial=self.runtime.poll('codex',{'sessions':self.sessions})['jobs'][0]
        self.runtime.receipt('codex',{'id':initial['id'],'status':'delivered'})
        self.store.apply('report',{'project_id':pid,'task_id':task['id'],'status':'running','evidence':'已领取'},'deepseek')
        self.assertEqual(self.runtime.poll('codex',{'sessions':self.sessions})['jobs'],[])
        self.store.apply('report',{'project_id':pid,'task_id':task['id'],'status':'review','evidence':'文件已写入并读回'},'deepseek')
        job=self.runtime.poll('codex',{'sessions':self.sessions})['jobs'][0]
        self.assertEqual(job['kind'],'report')
        self.assertIn('文件已写入并读回',job['prompt'])

    def test_live_issue_waits_for_idle_instead_of_interrupting_user_turn(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('issue_create',{'project_id':pid,'title':'接口有变更','body':'先讨论再继续','members':['deepseek']})
        active=[{**self.sessions[0],'status':'running'}]
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':active})['jobs'],[])
        job=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'][0]
        self.assertEqual(job['mode'],'followup')
        self.assertEqual(job['kind'],'issue_create')

    def test_offline_session_keeps_queued_task_and_receipt_is_idempotent(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('task_create',{'project_id':pid,'title':'写入文件','acceptance':'实际读回','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        self.runtime.poll('deepseek',{'sessions':[{**self.sessions[0],'status':'running'}]})
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':[]})['jobs'],[])
        job=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'][0]
        receipt={'id':job['id'],'status':'delivered'}
        self.runtime.receipt('deepseek',receipt)
        self.assertTrue(self.runtime.receipt('deepseek',receipt)['saved'])
        with self.assertRaises(Invalid):
            self.runtime.receipt('deepseek',{'id':job['id'],'status':'failed'})

    def test_model_selection_waits_for_idle_and_does_not_resume_paused_work(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('pause',{'project_id':pid,'paused':True})
        choice={'provider':'native-provider','model':'buddy/deepseek-v4.1-flash'}
        request=self.runtime.model_command(pid,'deepseek','model-select',choice)
        active=[{**self.sessions[0],'status':'running'}]
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':active})['jobs'],[])
        job=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'][0]
        self.assertEqual(job['id'],request['id'])
        self.assertEqual(job['selection'],choice)
        self.assertTrue(self.store.snapshot()['projects'][0]['paused'])
        with self.assertRaises(Invalid):
            self.runtime.model_command(pid,'deepseek','model-select',{'model':'missing-provider'})

    def test_members_sharing_native_model_do_not_start_concurrently(self):
        pid=self.project['id']
        other=self.store.apply('agent_save',{'name':'监督测试','app':'dsh'})
        self.store.apply('assign',{'project_id':pid,'assignments':{'planner':other['id'],'executor':'deepseek','reporter':other['id']}})
        catalog=[dict(self.sessions[0],nextProvider='gateway',nextModel='buddy/model'),dict(self.sessions[0],id='native-two',nextProvider='gateway',nextModel='buddy/model')]
        for agent,session in [('deepseek','native-one'),(other['id'],'native-two')]:
            self.runtime.poll(agent,{'sessions':catalog})
            self.runtime.bind(pid,agent,session)
        self.store.apply('task_create',{'project_id':pid,'title':'原生执行','acceptance':'独立检查','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        first=self.runtime.poll('deepseek',{'sessions':catalog})['jobs'][0]
        self.assertEqual(self.runtime.poll(other['id'],{'sessions':catalog})['jobs'],[])
        self.runtime.receipt('deepseek',{'id':first['id'],'status':'delivered'})
        # The native enqueue can precede the next running-status heartbeat.
        self.assertEqual(self.runtime.poll(other['id'],{'sessions':catalog})['jobs'],[])
        later=time.time()+4
        with patch('relay.runtime.time.time',return_value=later):
            self.assertEqual(len(self.runtime.poll(other['id'],{'sessions':catalog})['jobs']),1)


    def test_project_chat_reaches_bound_native_session(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('project_message',{'project_id':pid,'body':'没有议题也能通知到原应用'})
        job=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'][0]
        self.assertEqual(job['kind'],'project_message')
        self.assertIn('没有议题也能通知到原应用',job['prompt'])
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'],[])

    def test_project_chat_stops_after_four_member_replies(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        for n in range(3):
            self.store.apply('project_message',{'project_id':pid,'body':f'监督回复 {n}'},'codex')
            job=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'][0]
            self.runtime.receipt('deepseek',{'id':job['id'],'status':'delivered'})
        self.store.apply('project_message',{'project_id':pid,'body':'监督第四条'},'codex')
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'],[])
        self.store.apply('project_message',{'project_id':pid,'body':'用户回复'})
        self.assertEqual(len(self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs']),1)


    def test_user_bound_session_is_registered_and_only_unbound(self):
        pid=self.project['id']
        self.runtime.bind(pid,'deepseek','native-one')
        resource=self.runtime.snapshot()['resources'][0]
        self.assertEqual((resource['origin'],resource['status']),('user-bound','active'))
        self.store.apply('project_close',{'project_id':pid,'retention_days':5})
        plan=self.runtime.close_project(pid)
        self.assertEqual(len(plan['unbound']),1)
        self.assertEqual(plan['archived'],[])
        self.assertEqual(self.runtime.snapshot()['bindings'],[])
        self.assertEqual(self.runtime.snapshot()['resources'][0]['status'],'unbound')
        self.assertEqual([j for j in self.runtime.snapshot()['jobs'] if j['detail'].get('kind')=='archive-session'],[])

    def test_panel_created_session_is_archived_through_the_native_interface(self):
        pid=self.project['id']
        request=self.runtime.create_session(pid,'deepseek')
        self.runtime.poll('deepseek',{'sessions':self.sessions})
        self.runtime.created('deepseek',{'id':request['id'],'session':'native-created'})
        resource=self.runtime.snapshot()['resources'][0]
        self.assertEqual(resource['origin'],'panel-created')
        self.store.apply('project_close',{'project_id':pid,'retention_days':3})
        plan=self.runtime.close_project(pid)
        self.assertEqual(len(plan['archived']),1)
        archive=self.runtime.poll('deepseek',{'sessions':[{**self.sessions[0],'id':'native-created'}]})['jobs'][0]
        self.assertEqual(archive['kind'],'archive-session')
        self.assertEqual(archive['archive_session'],'native-created')
        self.runtime.receipt('deepseek',{'id':archive['id'],'status':'delivered'})
        self.assertEqual(self.runtime.snapshot()['resources'][0]['status'],'archived')
        self.assertEqual(self.runtime.snapshot()['resources'][0]['detail'],'原应用已完成归档')

    def test_offline_host_defers_archive_and_restart_marks_retry(self):
        pid=self.project['id']
        request=self.runtime.create_session(pid,'deepseek')
        self.runtime.poll('deepseek',{'sessions':self.sessions})
        self.runtime.created('deepseek',{'id':request['id'],'session':'native-created'})
        self.store.apply('project_close',{'project_id':pid,'retention_days':3})
        later=time.time()+60
        with patch('relay.runtime.time.time',return_value=later):
            plan=self.runtime.close_project(pid)
        self.assertEqual(len(plan['retry']),1)
        self.assertEqual(self.runtime.snapshot()['resources'][0]['status'],'retry')
        Runtime(self.store)
        self.assertEqual(self.runtime.snapshot()['resources'][0]['status'],'retry')


    def test_rebinding_revokes_queued_work_and_bumps_generation(self):
        pid=self.project['id']
        other=[{**self.sessions[0],'id':'native-two'}]
        self.runtime.poll('deepseek',{'sessions':self.sessions+other})
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('task_create',{'project_id':pid,'title':'执行检查','acceptance':'读回文件','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        self.assertEqual(self.runtime.snapshot()['bindings'][0]['generation'],1)
        # A busy native session gets the delivery queued but not leased, which is
        # exactly the undelivered work a switch is allowed to revoke.
        busy=[{**self.sessions[0],'status':'running'},other[0]]
        self.assertFalse(self.runtime.poll('deepseek',{'sessions':busy})['jobs'])
        queued=[j for j in self.runtime.snapshot()['jobs'] if j['status']=='queued' and j['detail'].get('kind')=='task_queued']
        self.assertTrue(queued)
        # The old session stops advertising itself: its queued work is revoked,
        # while a still-running session would have refused the switch.
        self.runtime.poll('deepseek',{'sessions':other})
        self.runtime.bind(pid,'deepseek','native-two')
        binding=self.runtime.snapshot()['bindings'][0]
        self.assertEqual(binding['session'],'native-two')
        self.assertEqual(binding['generation'],2)
        self.assertFalse(any(j['status'] in {'queued','leased'} for j in self.runtime.snapshot()['jobs']))
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':self.sessions+other})['jobs'],[])

    def test_rebinding_waits_while_the_old_session_is_still_running(self):
        pid=self.project['id']
        other=[{**self.sessions[0],'id':'native-two'}]
        self.runtime.poll('deepseek',{'sessions':self.sessions+other})
        self.runtime.bind(pid,'deepseek','native-one')
        self.runtime.poll('deepseek',{'sessions':[{**self.sessions[0],'status':'running'},other[0]]})
        with self.assertRaises(Invalid):
            self.runtime.bind(pid,'deepseek','native-two')
        self.assertEqual(self.runtime.snapshot()['bindings'][0]['session'],'native-one')
        self.runtime.poll('deepseek',{'sessions':self.sessions+other})
        self.runtime.bind(pid,'deepseek','native-two')
        self.assertEqual(self.runtime.snapshot()['bindings'][0]['generation'],2)

    def test_rebinding_refuses_while_execution_is_in_flight(self):
        pid=self.project['id']
        other=[{**self.sessions[0],'id':'native-two'}]
        self.runtime.poll('deepseek',{'sessions':self.sessions+other})
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('task_create',{'project_id':pid,'title':'执行检查','acceptance':'读回文件','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        delivered=self.runtime.poll('deepseek',{'sessions':self.sessions+other})['jobs']
        self.assertTrue(delivered)
        # The native app already has this call; a database cancel cannot undo it.
        with self.assertRaises(Invalid):
            self.runtime.bind(pid,'deepseek','native-two')
        self.assertEqual(self.runtime.snapshot()['bindings'][0]['session'],'native-one')
        # A receipt for the stale generation is recorded as uncertain, not delivered.
        self.runtime.receipt('deepseek',{'id':delivered[0]['id'],'status':'delivered','detail':'old session ran it'})
        job=next(j for j in self.runtime.snapshot()['jobs'] if j['id']==delivered[0]['id'])
        self.assertEqual(job['status'],'delivered')
        self.assertEqual(job['detail']['generation'],1)


    def test_receipt_from_a_previous_generation_is_uncertain(self):
        pid=self.project['id']
        other=[{**self.sessions[0],'id':'native-two'}]
        self.runtime.poll('deepseek',{'sessions':self.sessions+other})
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('task_create',{'project_id':pid,'title':'执行检查','acceptance':'读回文件','owner':'deepseek'})
        self.store.apply('dispatch',{'project_id':pid})
        delivered=self.runtime.poll('deepseek',{'sessions':self.sessions+other})['jobs']
        self.assertEqual(delivered[0]['generation'],1)
        # Simulate a switch that happened after the native call was handed over.
        with self.runtime.store.connect() as db:
            db.execute('UPDATE native_binding_meta SET generation=2 WHERE project=? AND agent=?',(pid,'deepseek'))
        self.runtime.receipt('deepseek',{'id':delivered[0]['id'],'status':'delivered','detail':'old binding ran it'})
        job=next(j for j in self.runtime.snapshot()['jobs'] if j['id']==delivered[0]['id'])
        self.assertEqual(job['status'],'uncertain')
        self.assertEqual(job['detail']['stale_generation'],1)


    def test_pending_notifications_coalesce_into_one_wake(self):
        pid=self.project['id']
        self.runtime.poll('deepseek',{'sessions':self.sessions})
        self.runtime.bind(pid,'deepseek','native-one')
        self.store.apply('project_message',{'project_id':pid,'body':'第一条'})
        self.store.apply('project_message',{'project_id':pid,'body':'第二条'})
        self.store.apply('project_message',{'project_id':pid,'body':'第三条'})
        busy=[{**self.sessions[0],'status':'running'}]
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':busy})['jobs'],[])
        jobs=self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs']
        self.assertEqual(len(jobs),1,jobs)
        self.assertEqual(len(jobs[0]['merged_events']),2,jobs[0].get('merged_events'))
        self.assertIn('第二条',jobs[0]['prompt'])
        states=[j['status'] for j in self.runtime.snapshot()['jobs'] if j['detail'].get('kind')=='project_message']
        self.assertEqual(states.count('coalesced'),2,states)
        # The merged wake keeps every event id, and reconnect does not re-wake.
        self.assertEqual(self.runtime.poll('deepseek',{'sessions':self.sessions})['jobs'],[])


if __name__=='__main__':
    unittest.main()
