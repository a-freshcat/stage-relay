"""End-to-end recycle round trip on the packaged exe with an isolated data dir.

Creates a throwaway project, closes it, moves it to the recycle area, checks the
recycled listing, moves it back, and confirms both the workspace file and the
recovery file survive. No real project, session, or workspace is touched.
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / 'release' / 'StageRelay-0.3.18-codex-dsh' / 'Stage Relay' / 'Stage Relay.exe'
DATA = ROOT / 'qa' / 'recycle-e2e-data'
PORT = 4357

if DATA.exists():
    shutil.rmtree(DATA)
DATA.mkdir(parents=True)
workspace = DATA / 'ws'
workspace.mkdir()

proc = subprocess.Popen([str(EXE), '--port', str(PORT), '--data-dir', str(DATA)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def call(path, payload=None, cookie=None, method=None):
    url = f'http://127.0.0.1:{PORT}{path}'
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'))
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-Relay-Client', 'panel')
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.load(response)
            return response.status, body, response.headers.get('Set-Cookie', '').split(';')[0]
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode()), ''


results = []
def check(name, condition, detail=''):
    results.append((name, bool(condition), detail))


cookie = ''
status = body = None
for _ in range(40):
    try:
        status, body, _ = call('/api/health')
        if status == 200:
            break
    except OSError:
        time.sleep(0.5)
# The panel cookie is issued with the HTML page, so open it once.
with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/', timeout=10) as page:
    cookie = page.headers.get('Set-Cookie', '').split(';')[0]
check('取得面板会话 cookie', bool(cookie), cookie)
check('打包 exe 已启动', status == 200 and body.get('version') == '0.3.18-codex-dsh', body)

status, state, _ = call('/api/state', cookie=cookie)
project = None
status, body, _ = call('/api/action', cookie=cookie, payload={'action': 'project_create',
                                       'args': {'title': '回收往返验证', 'goal': '临时项目', 'workspace': str(workspace)}})
project = body['result']['id']
call('/api/action', cookie=cookie, payload={'action': 'task_create', 'args': {'project_id': project, 'title': '临时板块',
                                                       'owner': 'deepseek', 'acceptance': '结束即可'}})
call('/api/action', cookie=cookie, payload={'action': 'project_message', 'args': {'project_id': project, 'body': '回收前的群聊'}})

status, body, _ = call('/api/action', cookie=cookie, payload={'action': 'project_close',
                                       'args': {'project_id': project, 'retention_days': 3}})
check('项目已结束', body.get('result', {}).get('closed') is True, body.get('error'))

status, body, _ = call('/api/action', cookie=cookie, payload={'action': 'project_forget', 'args': {'project_id': project}})
recover = Path(body['result']['recover'])
check('移入回收区成功', status == 200 and recover.is_file(), body)
check('工作区文件未被删除', workspace.is_dir())

status, listing, _ = call('/api/projects/recycled', cookie=cookie)
ids = [r['id'] for r in listing['projects']]
check('回收区列出这条记录', project in ids, ids)
row = next(r for r in listing['projects'] if r['id'] == project)
check('回收区记录带规模信息', row['tasks'] == 1 and row['chat'] == 1, row)

status, storage, _ = call('/api/settings/storage', cookie=cookie)
check('存储接口可用', status == 200 and isinstance(storage['storage'], list), storage)
names = [r['project'] for r in storage['storage']]
check('已回收项目出现在占用列表', project in names, names)

status, body, _ = call('/api/action', cookie=cookie, payload={'action': 'project_restore', 'args': {'project_id': project}})
check('移回列表成功', status == 200 and body['result']['restored'] == project, body)
check('可恢复文件仍保留', recover.is_file())

status, state, _ = call('/api/state', cookie=cookie)
back = next((p for p in state['projects'] if p['id'] == project), None)
check('项目回到列表且保留群聊与板块',
      back is not None and len(back['chat']) == 1 and back['tasks'][0]['title'] == '临时板块', back and back['title'])

status, body, _ = call('/api/action', cookie=cookie, payload={'action': 'project_restore', 'args': {'project_id': project}})
check('重复移回被拒绝', status == 400, body)

proc.terminate()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()

passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f' :: {detail}'))
print(json.dumps({'passed': passed, 'total': len(results)}, ensure_ascii=False))
sys.exit(0 if passed == len(results) else 1)
