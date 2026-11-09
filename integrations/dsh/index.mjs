import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';

export const name = 'stage-relay-native';
export const inject = ['agents', 'sessions', 'sessionController', 'llm'];

export async function apply(ctx, config) {
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(config.panelUrl)) throw new Error('Stage Relay requires a loopback panel URL');
  const {createUserMessage} = await import(pathToFileURL(join(config.runtimeRoot,'node_modules/@deepseek-ai/dsh-llm/lib/index.js')).href);
  let closed=false, busy=false;
  let observed=new Set();
  let paused=new Set();
  const outbox=[];
  async function rpc(action,args) {
    const credentials=JSON.parse(await readFile(config.credentialsFile,'utf8'));
    const token=credentials[config.agentId];
    if(!token) throw new Error('Stage Relay member credentials unavailable');
    const response=await fetch(config.panelUrl+'/bridge',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({action,args}),signal:AbortSignal.timeout(5000)});
    const value=await response.json();
    if(!response.ok) throw new Error(value.error || 'Stage Relay rejected request');
    return value.result;
  }
  ctx.on('session/event',(session,event)=>{
    if(!observed.has(session.id)) return;
    if(!['request/header','tool/call','tool/result','assistant/message','turn/start','turn/end'].includes(event.type)) return;
    const original=event.data || {};
    let data={};
    if(event.type==='request/header') data={provider:original.header?.config?.provider,model:original.header?.config?.model,tools:original.header?.tools?.map(t=>t.name)};
    if(event.type==='tool/call') data={name:original.name,id:original.callId};
    if(event.type==='tool/result') data={id:original.message?.toolCallId || original.message?.source?.callId,isError:original.message?.isError};
    if(event.type==='assistant/message') data={text:(original.message?.content || []).filter(c=>c.type==='text').map(c=>c.text).join('\n'),model:original.message?.source?.model};
    if(event.type==='turn/end') data={reason:original.reason};
    outbox.push({session:session.id,seq:event.seq,event:{type:event.type,data}});
    if(outbox.length>1000) outbox.shift();
  });
  function resolveWorkspaceRegistry() {
    // The service is provided by @deepseek-ai/dsh-workspace on an ancestor
    // context, so plain property access resolves it. The reflection fallback
    // covers contexts where the declaration form is required, and both are
    // guarded so a missing service degrades to a failed receipt, not a crash.
    try { const direct=ctx.workspaceRegistry; if(direct?.archiveSession) return direct; } catch {}
    try { return ctx.reflect?.get?.('workspaceRegistry', false) ?? null; } catch { return null; }
  }
  async function tick() {
    if(closed || busy) return;
    busy=true;
    try {
      const agents=ctx.agents.list();
      const sessions=agents.map(agent=>{
        const header=agent.session.requestHeader?.();
        // Read the original controller's existing cache without installing a
        // selection on unrelated or child agents.
        const next=ctx.sessionController.agents?.selections?.get(agent)?.current;
        return {id:agent.id,title:agent.session.header.title || agent.id,workspace:agent.session.header.cwd || '',status:agent.status,model:header?.config?.model || null,provider:header?.config?.provider || null,nextModel:next?.model || null,nextProvider:next?.provider || null,modelSource:header ? 'native-request-header' : 'unconfirmed'};
      });
      const result=await rpc('runtime_poll',{app:'dsh',sessions});
      observed=new Set(result.observed_sessions);
      const nextPaused=new Set(result.paused_sessions || []);
      for(const id of nextPaused) {
        if(!paused.has(id)) ctx.agents.get(id)?.cancel({kind:'user'});
      }
      paused=nextPaused;
      while(outbox.length) {await rpc('runtime_event',outbox[0]);outbox.shift();}
      for(const job of result.jobs) {
        if(job.kind==='model-catalog' || job.kind==='model-select') {
          let outcome;
          try {
            if(job.kind==='model-catalog') {
              const catalog=await ctx.sessionController.modelCatalog();
              // Reasoning effort is a real per-route capability: it is read from
              // the same model info DSH validates a request against, so an effort
              // offered here is one the adapter will accept, and a route with no
              // reasoning support offers none instead of a dead option.
              const models=[];
              for(const g of catalog.groups) {
                for(const m of g.models) {
                  const entry={provider:g.id,model:m.id,label:g.name+' / '+m.name};
                  try {
                    const info=await ctx.llm.resolveModelInfo?.(g.id,m.id);
                    const reasoning=info?.reasoning;
                    if(reasoning) {
                      entry.efforts=reasoning.efforts.map(e=>e.id);
                      entry.effortDefault=reasoning.defaultEffort;
                    }
                  } catch {}
                  models.push(entry);
                }
              }
              outcome={models:models.slice(0,1000)};
            } else {
              const agent=ctx.agents.get(job.session);
              if(!agent || agent.status==='running') throw new Error('Native session is not idle');
              const controller=ctx.sessionController;
              if(!controller.commands?.requireModel || !controller.agents?.selectForNextRequest) throw new Error('This DSH version does not support session-local model selection');
              // An explicitly requested effort is validated by resolveCallConfig
              // below: DSH rejects one the route does not support rather than
              // silently clamping it, and that rejection becomes a failed receipt.
              await controller.commands.requireModel(job.selection);
              const resolved=await ctx.llm.resolveCallConfig(job.selection);
              const selected={provider:resolved.provider,model:resolved.model};
              if(resolved.reasoningEffort) selected.reasoningEffort=resolved.reasoningEffort;
              await controller.agents.serializeImageAdmission(agent,async()=>controller.agents.selectForNextRequest(agent,selected));
              outcome={selected};
            }
          } catch(error) {
            await rpc('runtime_receipt',{id:job.id,status:'failed',detail:String(error)});
            continue;
          }
          await rpc('runtime_receipt',{id:job.id,status:'delivered',result:outcome,detail:'Applied through native DSH services; application default preserved'});
          continue;
        }
        if(job.kind==='archive-session') {
          // Project close: archive only sessions this panel created, through the
          // application's own registry. Never delete the application database.
          try {
            const registry=resolveWorkspaceRegistry();
            if(!registry?.archiveSession) throw new Error('This DSH version does not expose workspaceRegistry.archiveSession to plugins');
            const outcome=await registry.archiveSession(job.archive_session,{stopActivity:true});
            observed.delete(job.archive_session);
            await rpc('runtime_receipt',{id:job.id,status:'delivered',result:outcome && typeof outcome==='object' ? outcome : {archived:true},detail:'Archived through the native workspaceRegistry; application data preserved'});
          } catch(error) {await rpc('runtime_receipt',{id:job.id,status:'failed',detail:String(error)});}
          continue;
        }
        if(job.kind==='create-session') {
          try {
            const created=await ctx.sessionController.create({cwd:job.workspace});
            await rpc('runtime_created',{id:job.id,session:created.sessionId});
          } catch(error) {await rpc('runtime_receipt',{id:job.id,status:'failed',detail:String(error)});}
          continue;
        }
        const agent=ctx.agents.get(job.session);
        try {
          if(!agent) throw new Error('The bound native session is no longer loaded');
          const input=createUserMessage({content:[{type:'text',text:job.prompt}],source:{kind:'user'}});
          if(job.mode==='steer') agent.steer(input);
          else agent.followup(input);
        } catch(error) {
          await rpc('runtime_receipt',{id:job.id,status:'failed',detail:String(error)});
          continue;
        }
        // A lost receipt after followup is uncertain, never a failed enqueue.
        // The panel's expired lease prevents automatic duplicate execution.
        await rpc('runtime_receipt',{id:job.id,status:'delivered',detail:`Enqueued through the existing DSH agent.${job.mode==='steer' ? 'steer' : 'followup'}()`});
      }
    } catch { /* Panel outages must not interrupt the original application. */ }
    finally {busy=false;}
  }
  const timer=setInterval(tick,1000);
  timer.unref?.();
  ctx.effect(()=>()=>{closed=true;clearInterval(timer);observed.clear();});
  void tick();
}
