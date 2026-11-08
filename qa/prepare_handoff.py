"""Prepare an un-dispatched local project, preserving the previous app's records."""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from relay.domain import Store

folder=(ROOT/'release/StageRelay-0.3.1-codex-dsh/Stage Relay').resolve()
assert folder.is_relative_to((ROOT/'release').resolve()) and (folder/'Stage Relay.exe').is_file()
data=folder/'data';data.mkdir(exist_ok=True)
previous=ROOT/'release/StageRelay-0.1.0/Stage Relay/data'
if not (data/'workspace.sqlite3').exists() and (previous/'workspace.sqlite3').exists():
    before=sqlite3.connect((previous/'workspace.sqlite3').as_uri()+'?mode=ro',uri=True)
    after=sqlite3.connect(data/'workspace.sqlite3');before.backup(after);after.close();before.close()
    for name in ('bridge-credentials.json','connection-ownership.json'):
        if (previous/name).is_file():shutil.copy2(previous/name,data/name)
    if (previous/'uploads').is_dir():shutil.copytree(previous/'uploads',data/'uploads')
store=Store(data/'workspace.sqlite3')
title='Stage Relay：Codex 与 DSH 续作任务组'
existing=next((p for p in store.snapshot()['projects'] if p['title']==title),None)
if existing:
    project=existing
else:
    project=store.apply('project_create',{'title':title,'workspace':str(ROOT),'goal':'继续完善本机 Stage Relay 项目。Codex 负责方案、监督和最终核验，DSH 负责实际代码执行。先读 '+str(ROOT/'handoff/DSH-CONTINUE.md')+' 与 PRODUCT_REQUIREMENTS.md；未知状态如实报告。DSH 可选择此前验证的 buddy/deepseek-v4.1-flash；保留原应用全局默认模型。此项目尚未派发，等待用户确认开始。','assignments':{'planner':'codex','executor':'deepseek','reporter':'codex'}})
    store.apply('task_create',{'project_id':project['id'],'title':'接手续作：完善任务结束与安全清理','owner':'deepseek','acceptance':'先阅读 handoff/DSH-CONTINUE.md 和 PRODUCT_REQUIREMENTS.md，检查现有代码后，在任务组发起方案议题并邀请 Codex；先明确范围，用户可修改下一板块。不删除真实数据、不关闭共享应用、不修改其他项目。实现经确认的结束/归档/资源登记与清理预览，用独立测试目录和测试会话验证。DSH 使用自己的原生工具、插件和适用的子 Agent 完成实现，通过 relay_report 提交 review 与真实文件/检查依据；Codex 独立检查后核验。不要直接覆盖正在运行的发布包，生成新版本目录。'})
    with store.connect() as db:
        state=json.loads(db.execute('SELECT body FROM workspace WHERE id=1').fetchone()[0])
        state['projects'].sort(key=lambda p:p['id']!=project['id'])
        db.execute('UPDATE workspace SET body=? WHERE id=1',(json.dumps(state,ensure_ascii=False),))
shutil.copy2(ROOT/'handoff/DSH-CONTINUE.md',folder/'DSH续作说明.md')
print(json.dumps({'project':project['id'],'workspace':project['workspace'],'dispatched':False,'data':str(data)},ensure_ascii=False))
