"""Client for the installed Codex native app-server, never a model API client."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .domain import Invalid, uid
from .native_apps import codex_executable


class NativeError(Exception):
    pass


class NativeRPC:
    def __init__(self, executable=None, packaged=None, data_dir=None):
        executable=executable or codex_executable()
        if not executable or Path(executable).suffix.lower()!='.exe':
            raise Invalid('未找到本机 Codex 原生可执行文件。请先安装 Codex。')
        if packaged is None:
            packaged=os.name=='nt' and (Path(executable).parents[2]/'AppxManifest.xml').is_file()
        self.incoming=queue.Queue()
        self.pending={}
        self.lock=threading.Lock()
        self.close_lock=threading.Lock()
        self.counter=0
        self.stderr=[]
        if packaged:
            from .windows_codex import PackagedCodexProcess
            if data_dir is None:
                import tempfile
                data_dir=Path(tempfile.gettempdir())/'stage-relay-native'
            self.process=PackagedCodexProcess(executable,data_dir)
        else:
            self.process=subprocess.Popen([str(executable),'app-server','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        threading.Thread(target=self._read,daemon=True).start()
        threading.Thread(target=self._errors,daemon=True).start()
        try:
            self.request('initialize',{'clientInfo':{'name':'stage_relay','title':'Stage Relay','version':'0.3.0'},'capabilities':{'experimentalApi':True}},timeout=20)
            self.send({'method':'initialized','params':{}})
        except Exception:
            self.close()
            raise

    def send(self, value):
        with self.lock:
            if self.process.poll() is not None:
                raise ConnectionError('Codex 原生进程已退出。')
            self.process.stdin.write(json.dumps(value,ensure_ascii=False)+'\n')
            self.process.stdin.flush()

    def request(self, method, params=None, timeout=45):
        with self.lock:
            self.counter+=1
            request_id=self.counter
            response=queue.Queue(maxsize=1)
            self.pending[request_id]=response
        try:
            self.send({'id':request_id,'method':method,'params':params or {}})
            try:
                value=response.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError('等待 Codex 原生接口回执超时：'+method) from None
            if 'error' in value:
                raise NativeError(str(value['error'].get('message','Codex rejected request')))
            return value['result']
        finally:
            with self.lock:
                self.pending.pop(request_id,None)

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    value=json.loads(line)
                except ValueError:
                    continue
                if 'method' in value:
                    if 'id' not in value and (value['method'].endswith('/delta') or value['method'].endswith('Delta')):
                        continue
                    self.incoming.put(value)
                else:
                    with self.lock:
                        pending=self.pending.get(value.get('id'))
                    if pending:
                        pending.put(value)
        finally:
            self.incoming.put({'method':'stage-relay/disconnected','params':{}})
            with self.lock:
                waiting=list(self.pending.values())
            for pending in waiting:
                try:
                    pending.put_nowait({'error':{'message':'Codex 原生连接已断开。'}})
                except queue.Full:
                    pass

    def _errors(self):
        for line in self.process.stderr:
            self.stderr.append(line.strip())
            self.stderr=self.stderr[-20:]

    def close(self):
        with self.close_lock:
            if self.process.poll() is None:
                try:
                    self.process.stdin.close()
                    self.process.wait(timeout=5)
                except (OSError,ValueError,subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:self.process.kill()


class CodexHost:
    """One native Codex harness per connected member, with durable own threads."""
    def __init__(self, server, member):
        self.server=server
        self.member=member
        self.agent=member['id']
        self.rpc=NativeRPC(data_dir=server.store.path.parent)
        self.sessions={}
        self.active={}
        self.approvals={}
        self.seq={}
        self.error=''
        self.unavailable={}
        self.lock=threading.RLock()
        self.stopped=threading.Event()
        with server.store.connect() as db:
            rows=db.execute('SELECT id,workspace FROM codex_threads WHERE agent=? AND id IN (SELECT session FROM native_bindings WHERE agent=?)',(self.agent,self.agent)).fetchall()
        try:
            for session,workspace in rows:
                self._resume(session,workspace)
            self.thread=threading.Thread(target=self._loop,daemon=True,name='NativeCodex-'+self.agent)
            self.thread.start()
        except Exception:
            self.rpc.close()
            raise

    def _resume(self,session,workspace):
        try:
            result=self.rpc.request('thread/resume',{'threadId':session,'excludeTurns':True,'cwd':workspace},timeout=8)
            self._remember(result)
            with self.lock:self.unavailable.pop(session,None)
        except (NativeError,TimeoutError) as error:
            with self.lock:self.unavailable[session]=str(error)

    def retry_sessions(self):
        with self.lock:missing=set(self.unavailable)
        with self.server.store.connect() as db:
            rows=db.execute('SELECT id,workspace FROM codex_threads WHERE agent=?',(self.agent,)).fetchall()
        for session,workspace in rows:
            if session in missing:self._resume(session,workspace)

    def _remember(self,result):
        native=result['thread']
        session=native['id']
        entry={'id':session,'title':native.get('name') or 'Codex 原生工作会话','workspace':result.get('cwd') or native.get('cwd',''),'status':'idle','model':result.get('model'),'provider':result.get('modelProvider') or native.get('modelProvider'),'modelSource':'native-thread-configuration','mode':'codex-native-harness'}
        with self.lock:self.sessions[session]=entry
        with self.server.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO codex_threads VALUES(?,?,?)',(session,self.agent,entry['workspace']))
            row=db.execute('SELECT max(seq) FROM native_activity WHERE agent=? AND session=?',(self.agent,session)).fetchone()
        self.seq[session]=row[0] or 0
        return entry

    def _event(self,session,kind,data):
        if session not in self.sessions:return
        self.seq[session]=self.seq.get(session,0)+1
        self.server.runtime.observe(self.agent,{'session':session,'seq':self.seq[session],'event':{'type':kind,'data':data}})

    def _notification(self,message):
        method=message['method'];p=message.get('params',{})
        session=p.get('threadId')
        if 'id' in message:
            if method=='currentTime/read':
                self.rpc.send({'id':message['id'],'result':{'currentTimeAt':int(time.time())}})
                return
            supported={'item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval','item/tool/requestUserInput','mcpServer/elicitation/request'}
            if session in self.sessions and method in supported:
                key=uid('approval')
                with self.lock:self.approvals[key]={'key':key,'id':message['id'],'method':method,'params':p,'session':session}
            else:
                self.error='未支持的原生交互：'+method
                self.rpc.send({'id':message['id'],'error':{'code':-32601,'message':'This client does not support this interaction. Report the blocker to the project owner.'}})
            return
        if method=='stage-relay/disconnected':
            raise ConnectionError('Codex 原生连接已退出，请重新连接。')
        if session not in self.sessions:return
        if method=='serverRequest/resolved':
            with self.lock:
                self.approvals={k:v for k,v in self.approvals.items() if v['id']!=p.get('requestId')}
        elif method=='turn/started':
            self.active[session]=p['turn']['id']
            with self.lock:self.sessions[session]['status']='running'
            self._event(session,'turn/start',{'turnId':p['turn']['id'],'source':'codex-app-server'})
        elif method=='turn/completed':
            turn=p['turn'];self.active.pop(session,None)
            with self.lock:
                self.sessions[session]['status']='idle'
                self.approvals={k:v for k,v in self.approvals.items() if v['session']!=session}
            error=turn.get('error')
            reason={'kind':'error','error':error} if error else {'kind':turn.get('status','completed')}
            self._event(session,'turn/end',{'turnId':turn['id'],'reason':reason})
        elif method in {'item/started','item/completed'}:
            item=p.get('item',{});kind=item.get('type','')
            if kind=='agentMessage' and method=='item/completed':
                self._event(session,'assistant/message',{'text':item.get('text',''),'id':item.get('id')})
            elif kind in {'commandExecution','fileChange','mcpToolCall','collabAgentToolCall','webSearch','imageGeneration'}:
                data={'id':item.get('id'),'name':item.get('tool') or kind,'nativeType':kind}
                if item.get('command'):data['command']=item['command']
                if method=='item/started':self._event(session,'tool/call',data)
                else:
                    data.update(status=item.get('status'),isError=item.get('status') in {'failed','declined'},output=str(item.get('aggregatedOutput',''))[:2000])
                    self._event(session,'tool/result',data)

    def _job(self,job):
        session=job.get('session')
        if job['kind']=='create-session':
            result=self.rpc.request('thread/start',{'cwd':job['workspace'],'sandbox':'workspace-write','approvalPolicy':'on-request','serviceName':'stage-relay','ephemeral':False})
            # An untouched empty thread may not yet have a durable rollout.
            # Persist native metadata before recording a resumable binding.
            self.rpc.request('thread/name/set',{'threadId':result['thread']['id'],'name':'Stage Relay 工作会话'})
            result['thread']['name']='Stage Relay 工作会话'
            entry=self._remember(result)
            self.server.runtime.created(self.agent,{'id':job['id'],'session':entry['id']})
            self._event(entry['id'],'session/config',{'model':entry['model'],'provider':entry['provider'],'source':'native-thread-configuration'})
            return
        if session not in self.sessions:raise NativeError('Bound Codex native thread unavailable')
        if job['kind']=='model-catalog':
            result=self.rpc.request('model/list',{'limit':100})
            models=[{'provider':self.sessions[session]['provider'],'model':m['model'],'label':m.get('displayName') or m['model']} for m in result.get('data',[])]
            self.server.runtime.receipt(self.agent,{'id':job['id'],'status':'delivered','result':{'models':models}})
        elif job['kind']=='model-select':
            selected=job['selection']
            if selected['provider']!=self.sessions[session]['provider']:raise NativeError('Changing the native model provider requires reconnecting the original harness configuration')
            available=self.rpc.request('model/list',{'limit':100})
            if selected['model'] not in {m['model'] for m in available.get('data',[])}:raise NativeError('Model is not available in the native Codex catalog')
            # This thread is already owned by this app-server. Reattaching it
            # just to change a model can contend with its active writer.
            if session in self.active:raise NativeError('请等当前原生执行结束后再切换模型。')
            with self.lock:self.sessions[session]['nextModel']=selected['model']
            self.server.runtime.receipt(self.agent,{'id':job['id'],'status':'delivered','result':{'selected':selected}})
        else:
            text=[{'type':'text','text':job['prompt'],'text_elements':[]}]
            if job.get('mode')=='steer' and session in self.active:
                self.rpc.request('turn/steer',{'threadId':session,'expectedTurnId':self.active[session],'input':text})
            else:
                options={'threadId':session,'input':text}
                if self.sessions[session].get('nextModel'):options['model']=self.sessions[session]['nextModel']
                result=self.rpc.request('turn/start',options)
                self.active[session]=result['turn']['id']
                with self.lock:self.sessions[session]['status']='running'
            self.server.runtime.receipt(self.agent,{'id':job['id'],'status':'delivered','detail':'Accepted by the installed Codex native harness'})

    def _loop(self):
        paused=set()
        try:
            while not self.stopped.is_set():
                while True:
                    try:message=self.rpc.incoming.get_nowait()
                    except queue.Empty:break
                    self._notification(message)
                with self.lock:catalog=[dict(s) for s in self.sessions.values()]
                result=self.server.runtime.poll(self.agent,{'app':'codex','source':'codex-app-server','sessions':catalog})
                next_paused=set(result['paused_sessions'])
                for session in next_paused-paused:
                    if session in self.active:
                        self.rpc.request('turn/interrupt',{'threadId':session,'turnId':self.active[session]})
                paused=next_paused
                for job in result['jobs']:
                    try:self._job(job)
                    except (TimeoutError,ConnectionError):
                        # A request may have been accepted: never mark it safe to replay.
                        self.error='原生操作回执不确定，请检查执行记录后再决定是否继续。'
                    except (NativeError,Invalid) as error:
                        self.server.runtime.receipt(self.agent,{'id':job['id'],'status':'failed','detail':str(error)})
                self.stopped.wait(0.7)
        except Exception as error:
            self.error=str(error)
        finally:
            self.rpc.close()

    def respond(self,key,answer):
        with self.lock:request=self.approvals.get(key)
        if not request:raise Invalid('原生请求已失效。')
        method=request['method']
        if method=='mcpServer/elicitation/request':
            if request['params'].get('mode')=='openai/userVerification' and answer.get('decision')=='accept':
                raise Invalid('此操作要求 Codex 原应用进行设备验证，本面板不能替代设备验证。')
            action=answer.get('decision')
            if action not in {'accept','decline','cancel'}:raise Invalid('请选择允许、拒绝或取消。')
            result={'action':action}
            if action=='accept':
                if request['params'].get('mode')=='url':
                    result['content']=None
                else:
                    properties=request['params'].get('requestedSchema',{}).get('properties',{})
                    content=answer.get('content',{})
                    if not isinstance(content,dict) or set(content)-set(properties):raise Invalid('原生表单回答无效。')
                    for field in request['params'].get('requestedSchema',{}).get('required',[]):
                        if field not in content:raise Invalid('请填写原生表单必填项。')
                    result['content']=content
        elif method=='item/tool/requestUserInput':
            questions=request['params'].get('questions',[])
            answers=answer.get('answers',{})
            if not isinstance(answers,dict) or any(not isinstance(answers.get(q['id']),str) or not answers[q['id']].strip() for q in questions):
                raise Invalid('请回答原生会话的问题。')
            result={'answers':{q['id']:{'answers':[answers[q['id']]]} for q in questions}}
        else:
            decision=answer.get('decision')
            if decision not in {'accept','decline','cancel'}:raise Invalid('请选择允许、拒绝或取消。')
            if method=='item/permissions/requestApproval':
                result={'permissions':request['params'].get('permissions',{}) if decision=='accept' else {},'scope':'turn'}
            else:result={'decision':decision}
        self.rpc.send({'id':request['id'],'result':result})
        with self.lock:self.approvals.pop(key,None)
        return {'answered':True}

    def snapshot(self):
        with self.lock:
            return {'agent':self.agent,'connected':self.rpc.process.poll() is None and self.thread.is_alive(),'error':self.error,'unavailable':dict(getattr(self,'unavailable',{})),'approvals':[{k:v for k,v in r.items() if k!='id'} for r in self.approvals.values()]}

    def close(self):
        self.stopped.set()
        for session,turn in list(self.active.items()):
            try:self.rpc.request('turn/interrupt',{'threadId':session,'turnId':turn},timeout=2)
            except Exception:pass
        self.thread.join(timeout=2)
        # Explicitly release our own native subscriptions before disconnecting.
        # Never unsubscribe a thread that failed to attach (another writer owns it).
        for session in list(self.sessions):
            try:self.rpc.request('thread/unsubscribe',{'threadId':session},timeout=3)
            except Exception:pass
        self.rpc.close()


class CodexManager:
    def __init__(self,server,enabled=True):
        self.server=server
        self.enabled=enabled
        self.hosts={}
        self.lock=threading.RLock()
        self.connect_locks={}
        self.connection_states={}
        self.closing=False
        with server.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS codex_threads(id TEXT PRIMARY KEY,agent TEXT,workspace TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS codex_enabled(agent TEXT PRIMARY KEY)')

    def connect(self,agent):
        if not self.enabled:raise Invalid('隔离测试配置不会启动真实 Codex。')
        member=next((a for a in self.server.store.snapshot()['agents'] if a['id']==agent and a['app']=='codex'),None)
        if not member:raise Invalid('请选择 Codex 成员。')
        owned=self.server.connections.owned.get(agent,{})
        if not member.get('setup',{}).get('configured') or owned.get('entry')!=self.server.connections.entry(member):
            raise Invalid('请先在当前面板中重新授权接入 Codex，登记对应的协作工具。')
        with self.lock:
            gate=self.connect_locks.setdefault(agent,threading.Lock())
        with gate:
            if self.closing:raise Invalid('面板正在关闭。')
            previous=self.hosts.get(agent)
            if previous and previous.snapshot()['connected']:
                previous.retry_sessions()
                return {'connected':True}
            if previous:previous.close()
            host=CodexHost(self.server,member)
            with self.lock:
                cancelled=self.closing
                if not cancelled:self.hosts[agent]=host
            if cancelled:
                host.close()
                raise Invalid('面板正在关闭。')
        with self.server.store.connect() as db:db.execute('INSERT OR IGNORE INTO codex_enabled VALUES(?)',(agent,))
        return {'connected':True,'mode':'codex-native-harness'}

    def request_connect(self,agent):
        if not self.enabled:raise Invalid('隔离测试配置不会启动真实 Codex。')
        if not any(a['id']==agent and a['app']=='codex' for a in self.server.store.snapshot()['agents']):
            raise Invalid('请选择 Codex 成员。')
        with self.lock:
            if self.closing:raise Invalid('面板正在关闭。')
            if self.connection_states.get(agent,{}).get('status')=='connecting':
                return {'status':'connecting'}
            self.connection_states[agent]={'status':'connecting','error':''}
        def run():
            try:
                self.connect(agent)
                value={'status':'connected','error':''}
            except Exception as error:
                value={'status':'failed','error':str(error)}
            with self.lock:self.connection_states[agent]=value
        threading.Thread(target=run,daemon=True,name='ConnectCodex-'+agent).start()
        return {'status':'connecting'}

    def restore(self):
        if not self.enabled:return
        with self.server.store.connect() as db:agents=[r[0] for r in db.execute('SELECT agent FROM codex_enabled')]
        for agent in agents:
            try:self.request_connect(agent)
            except Exception:pass

    def disconnect(self,agent):
        with self.lock:gate=self.connect_locks.setdefault(agent,threading.Lock())
        with gate:
            with self.lock:
                host=self.hosts.pop(agent,None)
                self.connection_states.pop(agent,None)
            if host:host.close()
            with self.server.store.connect() as db:db.execute('DELETE FROM codex_enabled WHERE agent=?',(agent,))

    def respond(self,agent,key,answer):
        with self.lock:host=self.hosts.get(agent)
        if not host:raise Invalid('Codex 原生连接已离线。')
        return host.respond(key,answer)

    def snapshot(self):
        with self.lock:
            hosts=dict(self.hosts);states={k:dict(v) for k,v in self.connection_states.items()}
        return [dict(hosts[agent].snapshot() if agent in hosts else {'agent':agent,'connected':False,'approvals':[]},**({'connectionStatus':states[agent]['status'],'connectionError':states[agent]['error']} if agent in states else {})) for agent in hosts.keys() | states.keys()]

    def close(self):
        with self.lock:self.closing=True;hosts=list(self.hosts.values())
        for host in hosts:host.close()

