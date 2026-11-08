"""Durable native-session bindings and delivery receipts, separate from model output."""

import json
import time
from . import brief, rules
from .domain import Invalid, now, uid


class Runtime:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS native_hosts(agent TEXT PRIMARY KEY, body TEXT NOT NULL, seen REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS native_bindings(project TEXT, agent TEXT, session TEXT, PRIMARY KEY(project,agent))')
            db.execute('CREATE TABLE IF NOT EXISTS native_jobs(id TEXT PRIMARY KEY, project TEXT, agent TEXT, session TEXT, event INTEGER, status TEXT, body TEXT, updated REAL, UNIQUE(event,agent))')
            db.execute('CREATE TABLE IF NOT EXISTS native_activity(agent TEXT, session TEXT, seq INTEGER, body TEXT, PRIMARY KEY(agent,session,seq))')
            # Every native session this panel touches is registered with its origin.
            # project-created sessions may be archived on close; sessions the user
            # bound from an existing app window are only unbound.
            db.execute('CREATE TABLE IF NOT EXISTS native_resources(id TEXT PRIMARY KEY, project TEXT, agent TEXT, session TEXT, origin TEXT, parent TEXT, created REAL, updated REAL, status TEXT, detail TEXT)')
            # Switching a member to another native session is a visible, counted
            # event: queued work is revoked and the new generation starts clean.
            db.execute('CREATE TABLE IF NOT EXISTS native_binding_meta(project TEXT, agent TEXT, generation INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY(project,agent))')
            # Incremental scan cursor: polling reads only events this member has
            # never evaluated, instead of rescanning the whole project history.
            db.execute('CREATE TABLE IF NOT EXISTS native_cursors(project TEXT, agent TEXT, seq INTEGER NOT NULL, PRIMARY KEY(project,agent))')
            # An event the discussion bound only *postpones* lives here, keyed by
            # its seq. It is retried on every poll and delivered as soon as the
            # bound lifts, without holding back later events for other issues.
            db.execute('CREATE TABLE IF NOT EXISTS native_deferred(project TEXT, agent TEXT, seq INTEGER NOT NULL, reason TEXT, since REAL NOT NULL, PRIMARY KEY(project,agent,seq))')
            # Rules recovery: the full collaboration rules are injected once per
            # binding generation (and again after a session goes quiet or comes
            # back from a gap), never on every wake.
            db.execute('CREATE TABLE IF NOT EXISTS native_rules(agent TEXT, session TEXT, generation INTEGER NOT NULL, full_sent INTEGER NOT NULL, full_confirmed INTEGER NOT NULL, compact INTEGER NOT NULL, PRIMARY KEY(agent,session))')
        self.reconcile()
        self.migrate_queued()

    def snapshot(self):
        with self.store.connect() as db:
            hosts=[dict(agent=r[0],**json.loads(r[1]),online=time.time()-r[2]<15) for r in db.execute('SELECT agent,body,seen FROM native_hosts')]
            bindings=[dict(project=r[0],agent=r[1],session=r[2],generation=(db.execute('SELECT generation FROM native_binding_meta WHERE project=? AND agent=?',(r[0],r[1])).fetchone() or [1])[0]) for r in db.execute('SELECT project,agent,session FROM native_bindings')]
            jobs=[dict(id=r[0],project=r[1],agent=r[2],session=r[3],status=r[4],detail=json.loads(r[5]),updated=r[6]) for r in db.execute('SELECT id,project,agent,session,status,body,updated FROM native_jobs ORDER BY updated DESC LIMIT 80')]
            activity=[dict(agent=r[0],session=r[1],seq=r[2],**json.loads(r[3])) for r in db.execute('SELECT agent,session,seq,body FROM native_activity ORDER BY rowid DESC LIMIT 120')]
            resources=[dict(id=r[0],project=r[1],agent=r[2],session=r[3],origin=r[4],parent=r[5],created=r[6],status=r[7],detail=r[8]) for r in db.execute('SELECT id,project,agent,session,origin,parent,created,status,detail FROM native_resources ORDER BY created')]
        return dict(hosts=hosts,bindings=bindings,jobs=jobs,activity=activity,resources=resources)

    @staticmethod
    def _generation(db, project, agent):
        row = db.execute('SELECT generation FROM native_binding_meta WHERE project=? AND agent=?', (project, agent)).fetchone()
        return row[0] if row else 1

    @staticmethod
    def _session_status(db, agent, session):
        row = db.execute('SELECT body FROM native_hosts WHERE agent=?', (agent,)).fetchone()
        if not row:
            return None
        for native in json.loads(row[0]).get('sessions', []):
            if native.get('id') == session:
                return native.get('status')
        return None

    @staticmethod
    def _decide_rules(db, agent, session, generation):
        """Return the rules block to inject, or '' when the session already has it.

        The full text goes out on the first wake of a binding generation, again
        if that first delivery was never confirmed after a short grace, and again
        after a long quiet gap (the native session may have been rebuilt or
        compacted without telling us). Every other wake carries only the digest,
        and `relay_rules` can always return the identical full text on demand.
        """
        row = db.execute('SELECT generation,full_sent,full_confirmed FROM native_rules WHERE agent=? AND session=?', (agent, session)).fetchone()
        now_ts = time.time()
        if not row or row[0] != generation:
            db.execute('INSERT INTO native_rules VALUES(?,?,?,?,?,?) ON CONFLICT(agent,session) DO UPDATE SET generation=excluded.generation,full_sent=excluded.full_sent,full_confirmed=0',
                       (agent, session, generation, now_ts, 0, 0))
            return rules.full(agent)
        _, full_sent, confirmed = row
        if not confirmed and now_ts - full_sent > 60:
            db.execute('UPDATE native_rules SET full_sent=? WHERE agent=? AND session=?', (now_ts, agent, session))
            return rules.full(agent)
        if now_ts - full_sent > 3600:
            db.execute('UPDATE native_rules SET full_sent=? WHERE agent=? AND session=?', (now_ts, agent, session))
            return rules.full(agent)
        return ''

    @staticmethod
    def _confirm_rules(db, agent, session):
        if db.execute('SELECT 1 FROM native_rules WHERE agent=? AND session=?', (agent, session)).fetchone():
            db.execute('UPDATE native_rules SET full_confirmed=1,compact=compact+1 WHERE agent=? AND session=?', (agent, session))

    def bind(self, project_id, agent_id, session_id):
        state=self.store.snapshot()
        p=next((p for p in state['projects'] if p['id']==project_id),None)
        if not p or p['demo']:
            raise Invalid('请选择真实项目。')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT body,seen FROM native_hosts WHERE agent=?',(agent_id,)).fetchone()
            if not row or time.time()-row[1]>15:
                raise Invalid('原应用连接未就绪，请先连接原生会话插件。')
            session=next((s for s in json.loads(row[0])['sessions'] if s['id']==session_id),None)
            if not session:
                raise Invalid('该会话已离线，请刷新。')
            # Native sessions must explicitly advertise their own working directory.
            from pathlib import Path
            if not session.get('workspace') or Path(session['workspace']).resolve()!=Path(p['workspace']).resolve():
                raise Invalid('原应用会话的工作区与项目不同，请在原应用打开对应项目后再绑定。')
            other=db.execute('SELECT project,agent FROM native_bindings WHERE session=? AND NOT(project=? AND agent=?)',(session_id,project_id,agent_id)).fetchone()
            if other:
                raise Invalid('此会话已经绑定其他职责，请使用独立会话。')
            current=db.execute('SELECT session FROM native_bindings WHERE project=? AND agent=?',(project_id,agent_id)).fetchone()
            if current and current[0]!=session_id:
                # A receipt only means "enqueued". If the old session is still
                # running, its turn may still act on the old binding, so the
                # switch waits for the original application to report idle.
                if self._session_status(db,agent_id,current[0])=='running':
                    raise Invalid('旧会话仍在执行中，请等它空闲后再换绑；不会把在途结果留给新会话。')
                # Delivered (leased) work already lives in the native app and can
                # not be revoked from here. Refuse the switch instead of pretending
                # a database status change stops it.
                in_flight=[]
                for job_id, raw in db.execute("SELECT id,body FROM native_jobs WHERE project=? AND agent=? AND status IN ('leased','uncertain')",(project_id,agent_id)).fetchall():
                    body=json.loads(raw)
                    kind=body.get('kind')
                    # Session-creation requests are inert window creation, and
                    # model/archive commands are panel-controlled; only real
                    # execution deliveries block a switch.
                    if kind=='create-session' or kind in {'model-catalog','model-select','archive-session'}:
                        continue
                    in_flight.append(job_id)
                if in_flight:
                    raise Invalid(f'该成员有 {len(in_flight)} 个在途原生执行尚未回执，请等回执或先处理后再换绑；不会丢弃已交付的调用。')
                # Only undelivered queued work is revoked, then the generation moves.
                for job_id, raw in db.execute("SELECT id,body FROM native_jobs WHERE project=? AND agent=? AND status='queued'",(project_id,agent_id)).fetchall():
                    if json.loads(raw).get('kind') in {'create-session','model-catalog','model-select','archive-session'}:
                        continue
                    db.execute("UPDATE native_jobs SET status='cancelled',updated=? WHERE id=?",(time.time(),job_id))
                db.execute('INSERT INTO native_binding_meta VALUES(?,?,?,?) ON CONFLICT(project,agent) DO UPDATE SET generation=native_binding_meta.generation+1,updated=excluded.updated',(project_id,agent_id,1,time.time()))
            else:
                db.execute('INSERT INTO native_binding_meta VALUES(?,?,?,?) ON CONFLICT(project,agent) DO NOTHING',(project_id,agent_id,1,time.time()))
            db.execute('INSERT OR REPLACE INTO native_bindings VALUES(?,?,?)',(project_id,agent_id,session_id))
            if not db.execute('SELECT 1 FROM native_resources WHERE project=? AND agent=? AND session=?',(project_id,agent_id,session_id)).fetchone():
                db.execute('INSERT INTO native_resources VALUES(?,?,?,?,?,?,?,?,?,?)',(uid('resource'),project_id,agent_id,session_id,'user-bound','',time.time(),time.time(),'active','用户在原应用中指定并绑定；结束时只解绑'))
        return dict(project=project_id,agent=agent_id,session=session_id)

    def create_session(self, project_id, agent_id, wait_for_connection=False, replace=False):
        state=self.store.snapshot()
        project=next((p for p in state['projects'] if p['id']==project_id and not p['demo']),None)
        agent=next((a for a in state['agents'] if a['id']==agent_id),None)
        if not project or not agent or agent['app'] not in {'dsh','codex'} or not project.get('workspace'):
            raise Invalid('请选择真实项目与 Codex / DSH 成员。')
        if project.get('closed'):raise Invalid('任务已结束，不能创建新的工作会话。')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT session FROM native_bindings WHERE project=? AND agent=?',(project_id,agent_id)).fetchone()
            if wait_for_connection:
                if existing:return {'session':existing[0],'status':'bound'}
            if existing and not replace:raise Invalid('项目已有工作会话；请明确选择使用独立会话，原记录会保留。')
            host=db.execute('SELECT seen FROM native_hosts WHERE agent=?',(agent_id,)).fetchone()
            if (not host or time.time()-host[0]>15) and not (wait_for_connection and agent.get('setup',{}).get('configured')):
                raise Invalid('原生应用连接尚未就绪。')
            if db.execute("SELECT 1 FROM native_jobs WHERE project=? AND agent=? AND session='' AND status IN ('queued','leased','uncertain')",(project_id,agent_id)).fetchone():
                raise Invalid('该成员已有创建会话请求，请等待结果。')
            job=uid('create')
            db.execute('INSERT INTO native_jobs VALUES(?,?,?,?,?,?,?,?)',(job,project_id,agent_id,'',None,'queued',json.dumps(dict(kind='create-session',workspace=project['workspace'],previous_session=existing[0] if existing else None),ensure_ascii=False),time.time()))
        return {'id':job,'status':'queued'}

    def created(self,agent,payload):
        session=payload.get('session')
        if not isinstance(session,str) or not session:
            raise Invalid('原生会话标识缺失。')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT project,body,status FROM native_jobs WHERE id=? AND agent=?',(payload.get('id'),agent)).fetchone()
            if not row or json.loads(row[1]).get('kind')!='create-session' or row[2]!='leased':
                raise Invalid('没有对应的用户创建授权。')
            detail=json.loads(row[1])
            old=db.execute('SELECT session FROM native_bindings WHERE project=? AND agent=?',(row[0],agent)).fetchone()
            previous=old[0] if old else None
            if previous!=detail.get('previous_session'):
                db.execute('INSERT INTO native_resources VALUES(?,?,?,?,?,?,?,?,?,?)',(uid('resource'),row[0],agent,session,'panel-created','',time.time(),time.time(),'unbound','创建期间绑定发生变化，新会话保留但未接管项目'))
                detail.update(created_session=session,error='绑定已变化，新会话未接管项目')
                db.execute("UPDATE native_jobs SET status='failed',body=?,updated=? WHERE id=?",(json.dumps(detail),time.time(),payload['id']))
                return {'session':session,'bound':False}
            if previous and previous!=session:
                if self._session_status(db,agent,previous)=='running':
                    db.execute('INSERT INTO native_resources VALUES(?,?,?,?,?,?,?,?,?,?)',(uid('resource'),row[0],agent,session,'panel-created','',time.time(),time.time(),'unbound','旧会话仍在执行，新会话未接管'))
                    detail.update(created_session=session,error='旧会话仍在执行，新会话未接管项目')
                    db.execute("UPDATE native_jobs SET status='failed',body=?,updated=? WHERE id=?",(json.dumps(detail),time.time(),payload['id']))
                    return {'session':session,'bound':False}
                in_flight=db.execute("SELECT COUNT(*) FROM native_jobs WHERE project=? AND agent=? AND session=? AND status IN ('leased','uncertain')",(row[0],agent,previous)).fetchone()[0]
                if in_flight:
                    db.execute('INSERT INTO native_resources VALUES(?,?,?,?,?,?,?,?,?,?)',(uid('resource'),row[0],agent,session,'panel-created','',time.time(),time.time(),'unbound',f'旧会话仍有 {in_flight} 个在途执行，新会话未接管'))
                    detail.update(created_session=session,error='旧会话有在途执行，新会话未接管项目')
                    db.execute("UPDATE native_jobs SET status='failed',body=?,updated=? WHERE id=?",(json.dumps(detail),time.time(),payload['id']))
                    return {'session':session,'bound':False}
                db.execute("UPDATE native_jobs SET status='cancelled' WHERE project=? AND agent=? AND session=? AND status='queued'",(row[0],agent,previous))
                db.execute("UPDATE native_resources SET status='unbound',detail=?,updated=? WHERE project=? AND agent=? AND session=?",('已改用独立工作会话；旧会话及记录保留，不自动归档',time.time(),row[0],agent,previous))
                # The created session is a switch too, so it moves the generation.
                db.execute('INSERT INTO native_binding_meta VALUES(?,?,?,?) ON CONFLICT(project,agent) DO UPDATE SET generation=native_binding_meta.generation+1,updated=excluded.updated',(row[0],agent,1,time.time()))
            else:
                db.execute('INSERT INTO native_binding_meta VALUES(?,?,?,?) ON CONFLICT(project,agent) DO NOTHING',(row[0],agent,1,time.time()))
            db.execute('INSERT OR REPLACE INTO native_bindings VALUES(?,?,?)',(row[0],agent,session))
            if not db.execute('SELECT 1 FROM native_resources WHERE project=? AND agent=? AND session=?',(row[0],agent,session)).fetchone():
                db.execute('INSERT INTO native_resources VALUES(?,?,?,?,?,?,?,?,?,?)',(uid('resource'),row[0],agent,session,'panel-created','',time.time(),time.time(),'active','面板为此项目创建的主会话；结束时按原应用接口归档'))
            detail=json.loads(row[1]);detail['created_session']=session
            db.execute("UPDATE native_jobs SET status='delivered',body=?,updated=? WHERE id=?",(json.dumps(detail),time.time(),payload['id']))
        return {'session':session}

    @staticmethod
    def _rebuild_merge_tail(db, body, merged_seqs):
        """Rebuild a job's prompt from its base plus the current merged id list.

        Rebuilding (rather than appending) is what makes a merge idempotent and
        what lets a recycled member be dropped without leaving its text behind.
        """
        base = body.get('prompt_base')
        if base is None:
            base = body.get('prompt', '')
            cut = base.find('\n\n同一会话还有 ')
            if cut != -1:
                base = base[:cut]
            body['prompt_base'] = base
        lines = []
        for seq in merged_seqs:
            found = db.execute('SELECT kind,actor,body FROM events WHERE seq=?', (seq,)).fetchone()
            if not found:
                continue
            lines.append(json.dumps({'seq': seq, 'kind': found[0], 'actor': found[1],
                                     'detail': brief.trimmed(json.loads(found[2]))}, ensure_ascii=False))
        if not lines:
            body['merged_events'] = []
            body['prompt'] = base
            return
        body['merged_events'] = [seq for seq in merged_seqs]
        shown = lines[:brief.MERGE_LINES]
        rest = merged_seqs[len(shown):]
        tail = (f'\n……另有 {len(rest)} 条已并入本次唤醒（seq {rest[0]}…{rest[-1]}），'
                '不要遗漏，用 relay_event 按 seq 读取全文。') if rest else ''
        body['prompt'] = (base + '\n\n同一会话还有 ' + str(len(lines)) +
                          ' 条待处理事件合并到本次唤醒，逐条核对后统一处理，不要重复回复相同内容（全文用 relay_event 按 seq 读取）：\n'
                          + '\n'.join(shown) + tail)

    def _revive_coalesced(self, db, project, agent, seq):
        """Turn a placeholder row back into its own delivery.

        A coalesced row keeps its event id but no prompt: it was folded into a
        head that has since been cancelled. Flipping that same row back to queued
        avoids touching the UNIQUE(event,agent) slot, so nothing can duplicate.
        """
        row = db.execute("SELECT id FROM native_jobs WHERE project=? AND agent=? AND event=? AND status='coalesced'",
                         (project, agent, seq)).fetchone()
        if not row:
            return False
        found = db.execute('SELECT kind,actor,body FROM events WHERE seq=?', (seq,)).fetchone()
        if not found:
            return False
        session_row = db.execute('SELECT session FROM native_bindings WHERE project=? AND agent=?',
                                 (project, agent)).fetchone()
        if not session_row:
            return False
        state = self.store.snapshot()
        project_state = next((p for p in state['projects'] if p['id'] == project), None)
        if not project_state:
            return False
        kind, actor, raw = found
        generation = self._generation(db, project, agent)
        wake = brief.brief(project_state, agent, kind, actor, json.loads(raw), seq=seq)
        wake['binding_generation'] = generation
        wake['rules_version'] = rules.VERSION
        prompt = rules.digest(agent) + '\n\n本次唤起的项目事件（其余状态按需读取）：\n' + json.dumps(wake, ensure_ascii=False)
        db.execute("UPDATE native_jobs SET status='queued',session=?,body=?,updated=? WHERE id=?",
                   (session_row[0],
                    json.dumps(dict(prompt=prompt, kind=kind, generation=generation, brief=wake, rules_full=False), ensure_ascii=False),
                    time.time(), row[0]))
        return True

    def migrate_queued(self):
        """Rebuild pre-optimisation queued prompts into the compact form.

        A job written by the previous version carries the whole project in its
        prompt. It has not been delivered yet, so nothing has been executed; but
        rewriting it must not lose the event it refers to. The job id, its event
        and its UNIQUE(event,agent) slot stay exactly as they are, and any event
        ids it had already absorbed are preserved. Delivered, leased, cancelled
        and uncertain rows are left alone: those are history or in flight.

        Idempotent: a job already carrying `brief` is skipped, so a restart or a
        second call costs nothing and changes nothing.
        """
        migrated = 0
        state = self.store.snapshot()
        projects = {p['id']: p for p in state['projects'] if not p['demo']}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for job_id, project, agent, session, event, raw in db.execute(
                    "SELECT id,project,agent,session,event,body FROM native_jobs WHERE status='queued'").fetchall():
                try:
                    body = json.loads(raw)
                except ValueError:
                    continue
                if body.get('kind') in {'create-session', 'model-catalog', 'model-select', 'archive-session'}:
                    continue
                if body.get('brief') or event is None:
                    continue
                project_state = projects.get(project)
                if not project_state:
                    continue
                found = db.execute('SELECT kind,actor,body FROM events WHERE seq=?', (event,)).fetchone()
                if not found:
                    continue
                kind, actor, event_raw = found
                detail = json.loads(event_raw)
                wake = brief.brief(project_state, agent, kind, actor, detail, seq=event)
                generation = self._generation(db, project, agent)
                wake['binding_generation'] = generation
                wake['rules_version'] = rules.VERSION
                head = rules.digest(agent)
                merged = list(body.get('merged_events') or [])
                prompt = head + '\n\n本次唤起的项目事件（其余状态按需读取）：\n' + json.dumps(wake, ensure_ascii=False)
                if merged:
                    prompt += ('\n\n同一会话还有 ' + str(len(merged)) + ' 条待处理事件合并到本次唤醒，'
                               '逐条核对后统一处理（全文用 relay_event 按 seq 读取）：seq '
                               + ', '.join(str(s) for s in merged))
                body.update(prompt=prompt, brief=wake, generation=generation,
                            rules_full=False, migrated_from_legacy_prompt=True)
                body.pop('prompt_base', None)
                db.execute('UPDATE native_jobs SET body=?,updated=? WHERE id=?',
                           (json.dumps(body, ensure_ascii=False), time.time(), job_id))
                migrated += 1
        return migrated

    def reconcile(self):
        """Re-check registrations after a crash instead of guessing by name.

        A leased job whose receipt never arrived becomes uncertain, never a
        replay. A resource stuck in 'archiving' without a live job is marked for
        retry so the next close or explicit retry re-checks the original app.
        """
        now_ts = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE native_jobs SET status='uncertain' WHERE status='leased' AND updated<?", (now_ts - 30,))
            active = set()
            for raw, in db.execute("SELECT body FROM native_jobs WHERE status IN ('queued','leased')"):
                try:
                    active.add(json.loads(raw).get('resource'))
                except ValueError:
                    continue
            for rid, in db.execute("SELECT id FROM native_resources WHERE status='archiving'").fetchall():
                if rid not in active:
                    db.execute("UPDATE native_resources SET status='retry',detail=?,updated=? WHERE id=?",
                               ('重启后未收到归档回执，需要按登记重新检查', now_ts, rid))

    def close_project(self, project_id):
        """Revoke pending work, then retire only the resources this panel owns.

        Panel-created sessions are archived through the original application's
        own interface. Sessions the user bound by hand are only unbound. Shared
        applications and plugins are never stopped or uninstalled here.
        """
        state = self.store.snapshot()
        project = next((p for p in state['projects'] if p['id'] == project_id), None)
        if not project:
            raise Invalid('任务不存在。')
        if not project.get('closed'):
            raise Invalid('请先在面板确认结束任务，再处理会话与资源。')
        plan = dict(cancelled=0, archived=[], unbound=[], retained=[], retry=[])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            plan['cancelled'] = db.execute("SELECT COUNT(*) FROM native_jobs WHERE project=? AND status IN ('queued','leased','uncertain')", (project_id,)).fetchone()[0]
            db.execute("UPDATE native_jobs SET status='cancelled' WHERE project=? AND status IN ('queued','leased','uncertain')", (project_id,))
            db.execute('DELETE FROM native_bindings WHERE project=?', (project_id,))
            pending = set()
            for raw, in db.execute("SELECT body FROM native_jobs WHERE project=? AND status IN ('queued','leased')", (project_id,)):
                try:
                    pending.add(json.loads(raw).get('resource'))
                except ValueError:
                    continue
            rows = db.execute('SELECT id,agent,session,origin,status FROM native_resources WHERE project=?', (project_id,)).fetchall()
            for rid, agent, session, origin, status in rows:
                item = dict(id=rid, session=session, origin=origin)
                if status in {'archived', 'unbound'}:
                    plan['retained'].append(item)
                    continue
                if rid in pending:
                    plan['retained'].append(item | {'status': 'archiving'})
                    continue
                if origin != 'panel-created':
                    db.execute("UPDATE native_resources SET status='unbound',detail=?,updated=? WHERE id=?",
                               ('已解除本项目绑定；原应用会话与共享插件保留', time.time(), rid))
                    plan['unbound'].append(item)
                    continue
                host = db.execute('SELECT seen FROM native_hosts WHERE agent=?', (agent,)).fetchone()
                if not host or time.time() - host[0] > 15:
                    db.execute("UPDATE native_resources SET status='retry',detail=?,updated=? WHERE id=?",
                               ('原应用连接离线，归档待重试', time.time(), rid))
                    plan['retry'].append(item)
                    continue
                job = uid('archive')
                body = dict(kind='archive-session', resource=rid, archive_session=session, reason='project-closed')
                db.execute('INSERT INTO native_jobs VALUES(?,?,?,?,?,?,?,?)', (job, project_id, agent, session, None, 'queued', json.dumps(body, ensure_ascii=False), time.time()))
                db.execute("UPDATE native_resources SET status='archiving',detail=?,updated=? WHERE id=?",
                           ('已请求原应用归档', time.time(), rid))
                plan['archived'].append(item | {'job': job})
        return plan

    def model_command(self, project, agent, operation, selection=None):
        if operation not in {'model-catalog','model-select'}:
            raise Invalid('模型操作无效。')
        state=self.store.snapshot()
        member=next((a for a in state['agents'] if a['id']==agent),None)
        if not member or member['app'] not in {'dsh','codex'}:
            raise Invalid('请选择 Codex 或 DSH 原生成员。')
        if operation=='model-select' and (not isinstance(selection,dict) or any(not isinstance(selection.get(k),str) or not 0<len(selection[k])<=300 for k in ('provider','model'))):
            raise Invalid('请选择原应用提供的模型。')
        with self.store.connect() as db:
            binding=db.execute('SELECT session FROM native_bindings WHERE project=? AND agent=?',(project,agent)).fetchone()
            host=db.execute('SELECT seen FROM native_hosts WHERE agent=?',(agent,)).fetchone()
            if not binding or not host or time.time()-host[0]>15:
                raise Invalid('请先绑定在线原生会话。')
            job=uid('model')
            body={'kind':operation}
            if operation=='model-select':
                body['selection']={k:selection[k] for k in ('provider','model')}
            db.execute('INSERT INTO native_jobs VALUES(?,?,?,?,?,?,?,?)',(job,project,agent,binding[0],None,'queued',json.dumps(body,ensure_ascii=False),time.time()))
        return {'id':job,'status':'queued'}

    def poll(self, agent, payload):
        sessions=payload.get('sessions',[])
        if not isinstance(sessions,list) or len(sessions)>300:
            raise Invalid('原生会话目录无效。')
        for s in sessions:
            if not isinstance(s,dict) or not isinstance(s.get('id'),str):
                raise Invalid('会话标识缺失。')
        state=self.store.snapshot()
        projects={p['id']:p for p in state['projects'] if not p['demo']}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO native_hosts VALUES(?,?,?)',(agent,json.dumps(dict(sessions=sessions,app=payload.get('app'),source=payload.get('source','native-plugin'))),time.time()))
            bindings=db.execute('SELECT project,session FROM native_bindings WHERE agent=?',(agent,)).fetchall()
            for project,session in bindings:
                p=projects.get(project)
                if not p or p['paused'] or session not in {s['id'] for s in sessions}:
                    continue
                # Only events never evaluated for this member are scanned, so a
                # long project does not cost more per wake than a new one.
                cursor_row=db.execute('SELECT seq FROM native_cursors WHERE project=? AND agent=?',(project,agent)).fetchone()
                if cursor_row:
                    cursor=cursor_row[0]
                else:
                    # Upgrade path. Seeding at MAX(native_jobs.event) would assume
                    # every earlier event was already evaluated, which is false for
                    # an event the discussion bound postponed. Seed at 0 instead:
                    # the sweep re-applies the same filters, UNIQUE(event,agent)
                    # makes already-delivered events free, postponed ones become
                    # deferred records, and permanently filtered ones drop out.
                    # Cost is one bounded pass per member, once.
                    cursor=0
                    db.execute('INSERT OR IGNORE INTO native_cursors VALUES(?,?,?)',(project,agent,cursor))
                deferred={row[0] for row in db.execute('SELECT seq FROM native_deferred WHERE project=? AND agent=?',(project,agent))}
                advanced=cursor
                generation_db=self._generation(db,project,agent)
                # Re-check postponed events first: one issue hitting the discussion
                # bound must not hold back later events for other issues or tasks.
                pending_seqs=sorted(deferred | {row[0] for row in db.execute(
                    'SELECT seq FROM events WHERE project=? AND seq>? ORDER BY seq',(project,cursor))})
                for seq in pending_seqs:
                    found=db.execute('SELECT kind,actor,body FROM events WHERE seq=?',(seq,)).fetchone()
                    if not found:
                        db.execute('DELETE FROM native_deferred WHERE project=? AND agent=? AND seq=?',(project,agent,seq))
                        advanced=max(advanced,seq)
                        continue
                    kind,actor,raw=found
                    detail=json.loads(raw)
                    if kind=='record_restore':
                        advanced=seq
                        continue
                    if agent not in detail.get('recipients',[]) or actor==agent or kind not in {'task_queued','message','project_message','issue_create','resolve','review','report','accept','summary_request','assign','invite','pause'}:
                        advanced=seq
                        continue
                    if kind=='task_queued' and any(t['id']==detail.get('task_id') and t.get('archived') for t in p['tasks']):
                        advanced=seq
                        continue
                    if kind=='issue_create' and any(i['id']==detail.get('issue_id') and i.get('archived') for i in p['issues']):
                        advanced=seq
                        continue
                    if kind=='project_message':
                        message_id=detail.get('message_id')
                        if message_id and any(m['id']==message_id and m.get('archived') for m in p.get('chat',[])):
                            advanced=seq
                            continue
                    if kind=='report' and detail.get('status')!='review':
                        advanced=seq
                        continue
                    if kind=='report' and agent!=p['assignments']['planner']:
                        advanced=seq
                        continue
                    if kind=='task_queued' and agent!=p['assignments']['planner'] and not any(t['id']==detail.get('task_id') and t['owner']==agent for t in p['tasks']):
                        advanced=seq
                        continue
                    if kind=='pause' and detail.get('paused') is not False:
                        advanced=seq
                        continue
                    issue_id=detail.get('issue_id')
                    issue=next((i for i in p['issues'] if i['id']==issue_id),None)
                    if issue and issue.get('archived'):
                        advanced=seq
                        continue
                    if kind=='message' and issue:
                        message_id=detail.get('message_id')
                        if message_id and any(m['id']==message_id and m.get('archived') for m in issue['messages']):
                            advanced=max(advanced,seq)
                            db.execute('DELETE FROM native_deferred WHERE project=? AND agent=? AND seq=?',(project,agent,seq))
                            continue
                        # Human messages reset the bounded autonomous discussion allowance.
                        latest_user=max((m for m in range(len(issue['messages'])) if issue['messages'][m]['actor']=='user'),default=-1)
                        if len(issue['messages'])-latest_user-1>=4:
                            # Postponed, not skipped, and only for this event: later
                            # events of other issues stay deliverable.
                            db.execute('INSERT OR IGNORE INTO native_deferred VALUES(?,?,?,?,?)',(project,agent,seq,'issue-discussion-bound',time.time()))
                            advanced=max(advanced,seq)
                            continue
                    if kind=='project_message':
                        # Same bound as issue chat: members stop after four consecutive
                        # replies until the user speaks again in the project thread.
                        chat=p.get('chat',[])
                        latest_user=max((n for n in range(len(chat)) if chat[n]['actor']=='user'),default=-1)
                        if len(chat)-latest_user-1>=4:
                            db.execute('INSERT OR IGNORE INTO native_deferred VALUES(?,?,?,?,?)',(project,agent,seq,'chat-discussion-bound',time.time()))
                            advanced=max(advanced,seq)
                            continue
                    # The wake carries the event and the object it references, not
                    # the accumulated project: evidence of finished boards, old
                    # chat and resolved issues stay readable through relay_inbox
                    # and relay_project, and are not re-sent on every wake.
                    wake=brief.brief(p,agent,kind,actor,detail,seq=seq)
                    wake['binding_generation']=generation_db
                    wake['rules_version']=rules.VERSION
                    rules_block=self._decide_rules(db,agent,session,generation_db)
                    head=rules_block or rules.digest(agent)
                    prompt=head+'\n\n本次唤起的项目事件（其余状态按需读取）：\n'+json.dumps(wake,ensure_ascii=False)
                    db.execute('INSERT OR IGNORE INTO native_jobs VALUES(?,?,?,?,?,?,?,?)',(uid('delivery'),project,agent,session,seq,'queued',json.dumps(dict(prompt=prompt,kind=kind,generation=generation_db,brief=wake,rules_full=bool(rules_block)),ensure_ascii=False),time.time()))
                    db.execute('DELETE FROM native_deferred WHERE project=? AND agent=? AND seq=?',(project,agent,seq))
                    advanced=max(advanced,seq)
                if advanced!=cursor:
                    db.execute('INSERT INTO native_cursors VALUES(?,?,?) ON CONFLICT(project,agent) DO UPDATE SET seq=excluded.seq',(project,agent,advanced))
            # Never replay an expired lease: a crash may have happened after native enqueue.
            db.execute("UPDATE native_jobs SET status='uncertain' WHERE agent=? AND status='leased' AND updated<?",(agent,time.time()-30))
            pending=[]
            loaded={s['id'] for s in sessions}
            busy={s['id'] for s in sessions if s.get('status')=='running'}
            scheduled=set()
            # Multiple bridges in one harness advertise the same live sessions.
            # Keep the freshest copy, then serialize a shared provider/model.
            catalog={}
            for host_raw, in db.execute('SELECT body FROM native_hosts WHERE seen>? ORDER BY seen',(time.time()-15,)):
                for native in json.loads(host_raw)['sessions']:
                    catalog[native['id']]=native
            def route(native):
                provider=native.get('nextProvider') or native.get('provider')
                model=native.get('nextModel') or native.get('model')
                return [provider,model] if provider and model else None
            # Several notifications can be pending for one session. They are one
            # logical wake: keep the first job, fold the rest into it with their
            # event ids preserved, so a reconnect never multiplies turns.
            mergeable={'message','project_message','assign','pause','invite','review','resolve','summary_request','accept'}
            groups={}
            for candidate in db.execute("SELECT id,project,session,body,event FROM native_jobs WHERE agent=? AND status='queued' ORDER BY event",(agent,)).fetchall():
                try:
                    queued_body=json.loads(candidate[3])
                except ValueError:
                    continue
                # Never fold across a binding generation: a job stamped with an
                # older generation belongs to a different session contract.
                if queued_body.get('generation') not in (None, self._generation(db,candidate[1],agent)):
                    continue
                if queued_body.get('kind') in mergeable and candidate[4] is not None:
                    groups.setdefault((candidate[1],candidate[2]),[]).append(candidate)
            absorbed=set()
            merged_into={}
            for key,rows in groups.items():
                if len(rows)<2:
                    continue
                head=rows[0]
                for extra in rows[1:]:
                    absorbed.add(extra[0])
                head_body=json.loads(head[3])
                # Union, never overwrite: a job merged in an earlier round keeps
                # every event id it already absorbed.
                merged_into[head[0]]=list(dict.fromkeys(list(head_body.get('merged_events') or [])+[r[4] for r in rows[1:]]))
            for job_id in absorbed:
                db.execute("UPDATE native_jobs SET status='coalesced',updated=? WHERE id=?",(time.time(),job_id))
            for row in db.execute("SELECT id,project,session,body,event FROM native_jobs WHERE agent=? AND status='queued' ORDER BY event",(agent,)).fetchall():
                body=json.loads(row[3])
                if row[0] in merged_into:
                    # Union, never overwrite: a job merged in an earlier round
                    # keeps every event id it already absorbed. Rebuilding from
                    # the stored base keeps a second round from stacking text.
                    self._rebuild_merge_tail(db, body, merged_into[row[0]])
                    # Persist immediately: this job may stay queued for a busy
                    # session, and the merge must survive until it is leased.
                    db.execute("UPDATE native_jobs SET body=?,updated=? WHERE id=?",(json.dumps(body,ensure_ascii=False),time.time(),row[0]))
                source=db.execute('SELECT kind,body FROM events WHERE seq=?',(row[4],)).fetchone() if row[4] is not None else None
                project_state=projects.get(row[1],{})
                # Re-check every member of this wake, not only its head: a merged
                # event whose task was recycled or whose issue was archived must
                # not ride along inside a wake that is otherwise still valid.
                wake_seqs=[row[4]] if row[4] is not None else []
                wake_seqs+= [seq for seq in (body.get('merged_events') or []) if seq not in wake_seqs]
                stale_members=[]
                for check_seq in wake_seqs:
                    found=db.execute('SELECT kind,body FROM events WHERE seq=?',(check_seq,)).fetchone()
                    if not found:
                        continue
                    check_kind, check_raw=found
                    check_detail=json.loads(check_raw)
                    check_issue=next((i for i in project_state.get('issues',[]) if i['id']==check_detail.get('issue_id')),None)
                    check_task=next((t for t in project_state.get('tasks',[]) if t['id']==check_detail.get('task_id')),None)
                    thread=project_state.get('chat',[]) if check_kind=='project_message' else check_issue.get('messages',[]) if check_issue else []
                    check_message=next((m for m in thread if m['id']==check_detail.get('message_id')),None)
                    if ((check_task is not None and check_task.get('archived') and check_kind=='task_queued')
                            or (check_issue is not None and check_issue.get('archived'))
                            or (check_message is not None and check_message.get('archived'))):
                        stale_members.append(check_seq)
                if stale_members:
                    remaining=[seq for seq in wake_seqs if seq not in stale_members]
                    if row[4] in stale_members:
                        # The head is gone. Any absorbed event that is still valid
                        # must not die with it: give it back its own job instead
                        # of cancelling the whole wake.
                        db.execute("UPDATE native_jobs SET status='cancelled' WHERE id=?",(row[0],))
                        for survivor in remaining:
                            self._revive_coalesced(db,project,agent,survivor)
                        continue
                    if not remaining:
                        db.execute("UPDATE native_jobs SET status='cancelled' WHERE id=?",(row[0],))
                        continue
                    if body.get('merged_events'):
                        body['merged_events']=[seq for seq in body['merged_events'] if seq not in stale_members]
                        self._rebuild_merge_tail(db, body, body['merged_events'])
                        db.execute("UPDATE native_jobs SET body=?,updated=? WHERE id=?",
                                   (json.dumps(body,ensure_ascii=False),time.time(),row[0]))
                if body.get('kind')=='task_queued' and projects.get(row[1],{}).get('assignments',{}).get('planner')!=agent:
                    event=db.execute('SELECT body FROM events WHERE seq=?',(row[4],)).fetchone()
                    task_id=json.loads(event[0]).get('task_id') if event else None
                    if not any(t['id']==task_id and t['owner']==agent for t in projects.get(row[1],{}).get('tasks',[])):
                        db.execute("UPDATE native_jobs SET status='cancelled' WHERE id=?",(row[0],))
                        continue
                if body.get('kind')=='report' and projects.get(row[1],{}).get('assignments',{}).get('planner')!=agent:
                    db.execute("UPDATE native_jobs SET status='cancelled' WHERE id=?",(row[0],))
                    continue
                model_command=body.get('kind') in {'model-catalog','model-select','create-session'}
                # Archiving is housekeeping, not model work: it must still run
                # after the user closed and paused the project.
                housekeeping=body.get('kind')=='archive-session'
                # A user may be speaking in this native session. Never steer a
                # collaborative notification into their active turn; keep it
                # queued until the original application reports the session idle.
                if row[2] in scheduled or (row[2] in busy and not housekeeping) or not projects.get(row[1]) or (projects[row[1]]['paused'] and not model_command and not housekeeping):
                    continue
                if body.get('kind') not in {'create-session','archive-session'} and row[2] not in loaded:
                    continue
                target_route=route(catalog.get(row[2],{}))
                if target_route and not model_command and not housekeeping and row[2] not in busy:
                    if any(s['id']!=row[2] and s.get('status')=='running' and route(s)==target_route for s in catalog.values()):
                        continue
                    warming=db.execute("SELECT session,body FROM native_jobs WHERE status='leased' OR (status='delivered' AND updated>?)",(time.time()-3,)).fetchall()
                    if any(session!=row[2] and json.loads(raw).get('route')==target_route for session,raw in warming):
                        continue
                    body['route']=target_route
                scheduled.add(row[2])
                # Stamp the binding generation into the delivery so a receipt or a
                # stale session can be checked against the switch it belonged to.
                body['generation']=self._generation(db,row[1],agent)
                db.execute("UPDATE native_jobs SET status='leased',body=?,updated=? WHERE id=?",(json.dumps(body,ensure_ascii=False),time.time(),row[0]))
                pending.append(dict(id=row[0],session=row[2],mode='followup',**body))
            authorized_sessions=[r[1] for r in bindings]
            paused_sessions=[session for project,session in bindings if projects.get(project,{}).get('paused')]
        return dict(jobs=pending,observed_sessions=authorized_sessions,paused_sessions=paused_sessions)

    def cancel_archived_record(self, project_id, kind, item_id, issue_id=None):
        """Revoke queued deliveries of a recycled record; leased work stays uncertain."""
        cancelled=0
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT j.id,e.kind,e.body FROM native_jobs j JOIN events e ON j.event=e.seq WHERE j.project=? AND j.status='queued'",(project_id,)).fetchall()
            for job_id,event_kind,raw in rows:
                detail=json.loads(raw)
                matching=(kind=='task' and event_kind=='task_queued' and detail.get('task_id')==item_id) or (kind=='issue' and detail.get('issue_id')==item_id) or (kind=='issue_message' and event_kind=='message' and detail.get('issue_id')==issue_id and detail.get('message_id')==item_id) or (kind=='project_message' and event_kind=='project_message' and detail.get('message_id')==item_id)
                if matching:
                    db.execute("UPDATE native_jobs SET status='cancelled',updated=? WHERE id=?",(time.time(),job_id))
                    cancelled+=1
        return cancelled

    def receipt(self,agent,payload):
        with self.store.connect() as db:
            row=db.execute('SELECT status,body FROM native_jobs WHERE id=? AND agent=?',(payload.get('id'),agent)).fetchone()
            if not row:
                raise Invalid('派发记录不存在。')
            if payload.get('status') not in {'delivered','failed'}:
                raise Invalid('回执状态无效。')
            if row[0] == payload['status']:
                return {'saved':True}
            detail=json.loads(row[1])
            stale_generation=False
            job_project=db.execute('SELECT project,session FROM native_jobs WHERE id=?',(payload.get('id'),)).fetchone()
            if row[0]=='leased' and job_project:
                current=self._generation(db,job_project[0],agent)
                if detail.get('generation') is not None and detail['generation']!=current:
                    stale_generation=True
            if row[0] != 'leased':
                if stale_generation:
                    return {'saved':True,'stale':True}
                raise Invalid('此任务不在等待原生应用回执的状态。')
            if stale_generation:
                # The native call belongs to a previous binding. Record it as
                # uncertain rather than delivered: this panel cannot claim what
                # that old session actually did.
                detail['receipt']=str(payload.get('detail',''))[:2000]
                detail['stale_generation']=detail.get('generation')
                db.execute("UPDATE native_jobs SET status='uncertain',body=?,updated=? WHERE id=?",(json.dumps(detail,ensure_ascii=False),time.time(),payload['id']))
                return {'saved':True,'stale':True}
            detail['receipt']=str(payload.get('detail',''))[:2000]
            if isinstance(payload.get('result'),dict):
                detail['result']=payload['result']
            db.execute('UPDATE native_jobs SET status=?,body=?,updated=? WHERE id=?',(payload['status'],json.dumps(detail,ensure_ascii=False),time.time(),payload['id']))
            # The native app actually took this wake, so the rules block it
            # carried is now part of that session's context. Later wakes send
            # only the digest until the session goes quiet or is rebound.
            session_row=db.execute('SELECT session FROM native_jobs WHERE id=?',(payload['id'],)).fetchone()
            if payload['status']=='delivered' and session_row and detail.get('rules_full'):
                self._confirm_rules(db,agent,session_row[0])
            resource=detail.get('resource')
            if resource:
                archived=payload['status']=='delivered'
                db.execute('UPDATE native_resources SET status=?,detail=?,updated=? WHERE id=?',
                           ('archived' if archived else 'retry',
                            '原应用已完成归档' if archived else '原应用未完成归档，可重试：'+str(payload.get('detail',''))[:300],
                            time.time(),resource))
        return {'saved':True}

    def observe(self,agent,payload):
        session=payload.get('session')
        with self.store.connect() as db:
            if not db.execute('SELECT 1 FROM native_bindings WHERE agent=? AND session=?',(agent,session)).fetchone():
                raise Invalid('只接收已授权绑定会话的活动。')
            event=payload.get('event',{})
            if event.get('type') not in {'request/header','session/config','tool/call','tool/result','assistant/message','turn/start','turn/end'}:
                raise Invalid('不支持的原生活动。')
            db.execute('INSERT OR IGNORE INTO native_activity VALUES(?,?,?,?)',(agent,session,int(payload['seq']),json.dumps(dict(type=event['type'],data=event.get('data',{}),created=now()),ensure_ascii=False)))
        return {'saved':True}
