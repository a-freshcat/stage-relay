"""Exercise the packaged 0.3.3 GUI release with isolated data.

Starts the real exe, then walks the new close + cleanup flow over HTTP so the
packaged build - not just the source tree - is proven to serve 0.3.3 behaviour.
Nothing outside qa/ is written and no real data directory is opened.
"""

import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent.parent
exe = (root / "release/StageRelay-0.3.5-codex-dsh/Stage Relay/Stage Relay.exe").resolve()
assert exe.is_relative_to((root / "release").resolve()) and exe.is_file(), exe
data = root / "qa/release-035-data"
data.mkdir(exist_ok=True)
workspace = data / "workspace"
workspace.mkdir(exist_ok=True)
port = 4347

startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 0
process = subprocess.Popen(
    [str(exe), "--port", str(port), "--data-dir", str(data), "--app-home", str(data / "fake-home")],
    startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
)


def request(method, path, value=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(value).encode() if value is not None else None
    connection.request(method, path, body, headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, payload, dict(response.getheaders()))
    connection.close()
    return result


try:
    until = time.monotonic() + 25
    while True:
        try:
            status, body, _ = request("GET", "/api/health")
            health = json.loads(body)
            break
        except OSError:
            if time.monotonic() > until:
                raise
            time.sleep(0.25)
    assert health["version"] == "0.3.5-codex-dsh", health

    _, _, headers = request("GET", "/")
    cookie = headers["Set-Cookie"].split(";")[0]
    panel = {"Cookie": cookie, "Content-Type": "application/json", "X-Relay-Client": "panel"}
    status, body, _ = request("GET", "/api/state", headers={"Cookie": cookie})
    assert status == 200, status
    project = json.loads(body)["projects"][0]

    status, body, _ = request(
        "POST", "/api/action",
        {"action": "project_create", "args": {"title": "打包检查", "goal": "验证结束与清理接口", "workspace": str(workspace.resolve())}},
        panel,
    )
    assert status == 200, body
    project = json.loads(body)["result"]

    status, body, _ = request(
        "POST", "/api/action",
        {"action": "project_message", "args": {"project_id": project["id"], "body": "打包版项目群聊"}},
        panel,
    )
    assert status == 200, body

    def action(name, args):
        code, raw, _ = request('POST','/api/action',{'action':name,'args':{'project_id':project['id'],**args}},panel)
        assert code==200,raw
        return json.loads(raw)['result']
    credentials=json.loads((data/'bridge-credentials.json').read_text(encoding='utf-8'))
    def agent_action(agent,name,args):
        code, raw, _=request('POST','/bridge',{'action':name,'args':{'project_id':project['id'],**args}}, {'Content-Type':'application/json','Authorization':'Bearer '+credentials[agent]})
        assert code==200,raw
        return json.loads(raw)['result']
    task=action('task_create',{'title':'审核交接','owner':'deepseek','acceptance':'保留原成果'})
    action('dispatch',{})
    agent_action('deepseek','report',{'task_id':task['id'],'status':'review','evidence':'原成果'})
    action('assign',{'assignments':{'planner':'antigravity','executor':'deepseek','reporter':'codex'}})
    late=agent_action('codex','review',{'task_id':task['id'],'approved':True,'evidence':'旧核验','review_epoch':0})
    assert late['reference_only'] is True
    accepted=agent_action('antigravity','review',{'task_id':task['id'],'approved':True,'evidence':'新核验','review_epoch':1})
    assert accepted['status']=='done' and accepted['evidence']=='原成果'
    print(json.dumps({'reviewer_handoff':True,'late_result_reference_only':True,'original_evidence_preserved':True}))

    action('pause',{'paused':True})
    agent_action('codex','runtime_poll',{'sessions':[{'id':'occupied-fixture','workspace':str(workspace.resolve())}]})
    code,raw,_=request('POST','/api/runtime/bind',{'project_id':project['id'],'agent_id':'codex','session_id':'occupied-fixture'},panel)
    assert code==200,raw
    code,raw,_=request('POST','/api/runtime/create',{'project_id':project['id'],'agent_id':'codex','replace':True},panel)
    assert code==200,raw
    job=json.loads(raw)['id']
    jobs=agent_action('codex','runtime_poll',{'sessions':[]})['jobs']
    assert [j['id'] for j in jobs]==[job],jobs
    agent_action('codex','runtime_created',{'id':job,'session':'independent-fixture'})
    _,raw,_=request('GET','/api/state',headers={'Cookie':cookie})
    state=json.loads(raw)
    assert next(p for p in state['projects'] if p['id']==project['id'])['paused'] is True
    assert next(b for b in state['runtime']['bindings'] if b['project']==project['id'] and b['agent']=='codex')['session']=='independent-fixture'
    print(json.dumps({'independent_session_while_paused':True,'no_task_replay':True}))

    status, body, _ = request(
        "POST", "/api/project/close",
        {"project_id": project["id"], "retention_days": 3},
        panel,
    )
    assert status == 200, body
    closed = json.loads(body)
    assert closed["result"]["closed"] is True

    status, body, _ = request(
        "GET", f"/api/cleanup/preview?project={project['id']}",
        headers={"Cookie": cookie},
    )
    assert status == 200, body
    preview = json.loads(body)

    status, body, _ = request(
        "POST", "/api/cleanup/execute",
        {"project_id": project["id"], "confirm": True},
        panel,
    )
    assert status == 200, body

    print(json.dumps({
        "release_version": health["version"],
        "packaged_exe_checked": True,
        "project_closed": closed["result"]["closed"],
        "chat_messages": len(closed["result"]["chat"]),
        "planned_archive_or_unbind": len(closed["plan"]["archived"]) + len(closed["plan"]["unbound"]),
        "preview_items": len(preview["items"]),
        "preview_retained": len(preview["retained"]),
    }, ensure_ascii=False))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
