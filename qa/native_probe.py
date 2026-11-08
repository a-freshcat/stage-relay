"""Bounded real-DSH verification; adds/removes only a uniquely named bridge entry.

Run explicitly during integration verification, never during normal app startup.
"""
import json
import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from relay.domain import Store
from relay.httpd import PanelServer


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model')
    parser.add_argument('--timeout',type=int,default=420,choices=range(30,601))
    args=parser.parse_args()
    run = ROOT / 'qa' / ('native-probe-' + time.strftime('%Y%m%d-%H%M%S'))
    work = run / 'workspace'
    work.mkdir(parents=True)
    store = Store(run / 'data/state.sqlite3')
    member = store.apply('agent_save', dict(name='Stage Relay 原生验证', app='dsh'))
    server = PanelServer(('127.0.0.1', 4340), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = store.apply('project_create', dict(title='Stage Relay 原生执行验证',
        goal='仅操作本测试工作区。使用原应用的原生工具和子 agent 验证实际执行；不修改其他文件或配置。',
        workspace=str(work)))
    report = dict(run=str(run), member=member['id'], stages=[], complete=False)
    report_path = run / 'result.json'

    def save(stage):
        report['stages'].append(stage)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print(stage, flush=True)

    configured = False
    try:
        plan = server.connections.prepare(member)
        save('prepared native plugin and MCP config; existing rows preserved')
        server.connections.commit(plan['plan_id'], True)
        configured = True
        save('installed uniquely named test integration with backup')
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            hosts = server.runtime.snapshot()['hosts']
            if any(h['agent'] == member['id'] and h['online'] for h in hosts):
                break
            time.sleep(1)
        else:
            save('native plugin did not report online within 45 seconds; no task sent')
            return
        save('existing DSH desktop native plugin connected')
        server.runtime.create_session(project['id'], member['id'])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            bindings = server.runtime.snapshot()['bindings']
            if bindings:
                report['session'] = bindings[0]['session']
                break
            time.sleep(1)
        else:
            save('native session creation not acknowledged; no task sent')
            return
        save('native desktop session created with its original preset')
        if args.model:
            def wait_job(job):
                end=time.monotonic()+60
                while time.monotonic()<end:
                    found=next(j for j in server.runtime.snapshot()['jobs'] if j['id']==job['id'])
                    if found['status']=='delivered':
                        return found['detail']['result']
                    if found['status'] in {'failed','uncertain'}:
                        raise RuntimeError(found['detail'].get('receipt','Native control receipt uncertain'))
                    time.sleep(1)
                raise RuntimeError('Native model operation timed out')
            catalog=wait_job(server.runtime.model_command(project['id'],member['id'],'model-catalog'))
            matches=[m for m in catalog['models'] if m['model']==args.model or m['provider']+'/'+m['model']==args.model]
            if len(matches)!=1:
                raise RuntimeError('Requested model did not uniquely match the original DSH catalog')
            selected=wait_job(server.runtime.model_command(project['id'],member['id'],'model-select',matches[0]))
            report['selected_model']=selected['selected']
            save('original DSH confirmed the requested session-local model selection')
        task = store.apply('task_create', dict(project_id=project['id'], title='验证原生工具与子 agent',
            owner=member['id'], acceptance='先用 relay_report 报告 running。使用应用原生子 agent 委派计算 17*23，并回收结果；若原应用没有子 agent 工具则如实报告，禁止伪造。使用应用文件工具在当前工作区创建 native-evidence.json，内容包含 calculation=391、真实模型名称（未知则 null）、原生工具名称、子 agent 是否成功及真实标识。用文件工具读回验证，然后 relay_report 提交 review，附上文件路径和真实检查依据。仅操作当前测试工作区，不修改其他项目。'))
        store.apply('dispatch', {'project_id': project['id']})
        save('task dispatched to bound original agent; awaiting actual tools')
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            state = store.snapshot()
            current = next(p for p in state['projects'] if p['id'] == project['id'])
            snapshot = server.runtime.snapshot()
            report['activity'] = snapshot['activity']
            report['jobs'] = snapshot['jobs']
            report['task'] = current['tasks'][0]
            errors=[e for e in snapshot['activity'] if e['type']=='turn/end' and e.get('data',{}).get('reason',{}).get('kind')=='error']
            if errors:
                report['native_error']=errors[0]['data']['reason']['error']
                save('original harness reported an execution error; evidence retained without claiming success')
                break
            if current['tasks'][0]['status'] == 'review':
                report['complete'] = bool((work / 'native-evidence.json').is_file() and any(e['type'] == 'tool/call' for e in snapshot['activity']))
                save('native agent submitted review; execution evidence recorded')
                break
            time.sleep(2)
        else:
            save('bounded verification ended without completed review')
        report['evidence_exists'] = (work / 'native-evidence.json').is_file()
    finally:
        if report.get('session'):
            store.apply('pause', {'project_id': project['id'], 'paused': True})
            time.sleep(2)
        if configured:
            plan = server.connections.prepare(member, remove=True)
            server.connections.commit(plan['plan_id'], True)
            save('removed uniquely named test integration; other plugins preserved')
        server.shutdown()
        server.server_close()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print(str(report_path), flush=True)


if __name__ == '__main__':
    main()
