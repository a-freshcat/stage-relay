"""Real original-DSH conversation, execution, supervisor and summary verification."""
import json
import argparse
import sys
import threading
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from relay.domain import Store
from relay.httpd import PanelServer


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex',action='store_true')
    args=parser.parse_args()
    run=ROOT/'qa'/(('native-probe-codex-dsh-' if args.codex else 'native-probe-collaboration-')+time.strftime('%Y%m%d-%H%M%S'))
    work=run/'workspace';work.mkdir(parents=True)
    store=Store(run/'data/state.sqlite3')
    executor=store.apply('agent_save',{'name':'原生执行验证','app':'dsh'})
    planner=store.apply('agent_save',{'name':'原生监督汇总验证','app':'codex' if args.codex else 'dsh'})
    members=[executor,planner]
    project=store.apply('project_create',{'title':'原生自动协作验证','workspace':str(work),'goal':'独立的自动测试项目。只在当前工作区工作；工具使用各自身份的 Stage Relay 服务。由原生会话对话、执行、核验和汇总。', 'assignments':{'planner':planner['id'],'executor':executor['id'],'reporter':planner['id']}})
    server=PanelServer(('127.0.0.1',4340),store)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    installed=[]
    report={'complete':False,'stages':[],'project':project['id']}
    announced=set()
    def log(message):
        print(message,flush=True);report['stages'].append(message)
        (run/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    def wait(check,timeout=45):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            for host in server.codex.snapshot():
                for request in host['approvals']:
                    if request['key'] not in announced:
                        announced.add(request['key'])
                        report.setdefault('pending_native_requests',[]).append(request)
                        log('native confirmation required: '+request['method']+' / '+str(request['params'].get('mode'))+' / '+json.dumps(request['params'].get('requestedSchema',{}),ensure_ascii=False))
            found=check()
            if found:return found
            time.sleep(1)
        raise RuntimeError('Timed out waiting for original DSH: '+report['stages'][-1])
    def job_result(request):
        def ready():
            job=next(j for j in server.runtime.snapshot()['jobs'] if j['id']==request['id'])
            if job['status'] in {'failed','uncertain'}:raise RuntimeError(job['detail'].get('receipt',job['status']))
            return job['detail'].get('result',{}) if job['status']=='delivered' else None
        return wait(ready)
    def current():return next(p for p in store.snapshot()['projects'] if p['id']==project['id'])
    try:
        for member in members:
            plan=server.connections.prepare(member)
            setup=server.connections.commit(plan['plan_id'],True);installed.append(member)
            store.apply('setup_record',setup)
            if member['app']=='codex':server.codex.connect(member['id'])
            wait(lambda:any(h['agent']==member['id'] and h['online'] for h in server.runtime.snapshot()['hosts']))
            server.runtime.create_session(project['id'],member['id'])
            wait(lambda:any(b['agent']==member['id'] for b in server.runtime.snapshot()['bindings']))
            if member['app']=='dsh':
                catalog=job_result(server.runtime.model_command(project['id'],member['id'],'model-catalog'))
                matches=[m for m in catalog['models'] if m['model']=='buddy/deepseek-v4.1-flash']
                if len(matches)!=1:raise RuntimeError('Requested model not uniquely available')
                job_result(server.runtime.model_command(project['id'],member['id'],'model-select',matches[0]))
            log('native session and requested model confirmed: '+member['id'])
        issue=store.apply('issue_create',{'project_id':project['id'],'title':'测试议题：确认分工','body':'这是原生协作的自动测试议题。执行成员和监督成员各自用自己的 relay_send_message 回复一句确认职责（只回复一次）。这不是要求用户做真实产品决策。确认后等待测试脚本派发工作。','members':[m['id'] for m in members]})
        wait(lambda:all(any(m['actor']==a['id'] for m in current()['issues'][0]['messages']) for a in members),90)
        log('both original agents replied through their own issue tools')
        store.apply('resolve',{'project_id':project['id'],'issue_id':issue['id'],'note':'自动测试的用户操作夹具：分工已确认，可以执行下一步。'})
        store.apply('task_create',{'project_id':project['id'],'title':'创建协作验证文件','owner':executor['id'],'acceptance':'只由执行负责人使用原生文件工具创建 collaboration.txt，内容必须是 verified-native-coordination；读回验证并用自己的 relay_report 提交 review。监督负责人收到 review 后须用自己的文件工具独立读回检查，用自己的 relay_review 通过验收。监督负责人不要提前执行此板块，不要代替执行负责人汇报。'})
        store.apply('dispatch',{'project_id':project['id']})
        wait(lambda:current()['tasks'][0]['status']=='done',150)
        if (work/'collaboration.txt').read_text(encoding='utf8').strip()!='verified-native-coordination':
            raise RuntimeError('Independent file-content verification failed')
        log('executor wrote file; independent native supervisor accepted it')
        store.apply('summary_request',{'project_id':project['id']})
        wait(lambda:current()['summary'],90)
        report.update(complete=True,project_state=current(),activity=server.runtime.snapshot()['activity'])
        log('assigned native reporter submitted the final summary')
    except Exception as error:
        report['error']=str(error)
        report['project_state']=current()
        report['activity']=server.runtime.snapshot()['activity']
        log('verification incomplete: '+str(error))
    finally:
        store.apply('pause',{'project_id':project['id'],'paused':True});time.sleep(2)
        for member in installed:
            if member['app']=='codex':server.codex.disconnect(member['id'])
            plan=server.connections.prepare(member,remove=True);server.connections.commit(plan['plan_id'],True)
        server.shutdown();server.server_close()
        log('removed test integrations; original application plugins preserved')
        print(run/'result.json',flush=True)


if __name__=='__main__':main()
