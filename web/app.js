const $ = (s, root = document) => root.querySelector(s);
const esc = (value = '') => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  tasks:'M8 4H5a2 2 0 0 0-2 2v14h18V6a2 2 0 0 0-2-2h-3 M8 2v5h8V2 M8 13l3 3 5-5',
  users:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M16 4a4 4 0 0 1 0 8 M22 21v-2a4 4 0 0 0-3-3.87 M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  chat:'M21 11a8 8 0 0 1-8 8H8l-5 3V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z M7 9h.01 M12 9h.01 M17 9h.01',
  file:'M14 2H5a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9z M14 2v7h7 M7 13h10 M7 17h7',
  folder:'M22 20H2V5h8l2 3h10z',
  plus:'M12 5v14 M5 12h14',
  close:'M6 6l12 12 M18 6 6 18',
  edit:'m16 3 5 5 M3 21l5-1L21 7 17 3 4 16z',
  pause:'M8 4v16 M16 4v16',
  play:'m7 4 14 8-14 8z',
  check:'m5 12 4 4L19 6',
  clock:'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0 M12 6v6l4 2',
  circle:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  arrow:'M7 17 17 7 M7 7h10v10',
  external:'M14 3h7v7 M21 3 10 14 M10 3H3v18h18v-7',
  chevron:'m6 9 6 6 6-6',
  info:'M12 11v6 M12 7h.01 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  archive:'M3 4h18v4H3z M5 8v12h14V8 M10 12h4',
  layers:'m12 3 10 6-10 6L2 9z M2 14l10 6 10-6 M2 19l10 6 10-6',
  screen:'M2 3h20v14H2z M12 17v4 M8 21h8',
  send:'m22 2-7 20-4-9-9-4z M22 2 11 13',
  attach:'m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9',
  image:'M3 3h18v18H3z M3 17l6-6 4 4 3-3 5 5 M8 7h.01',
  link:'m10 13 4-4 M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0 M16 8l2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
  download:'M12 3v12 m-5-5 5 5 5-5 M4 17v4h16v-4',
  copy:'M8 8h13v13H8z M16 8V3H3v13h5',
};
const icon = name => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.circle}"/></svg>`;
const roles = {planner:'方案设计 · 进度监督', executor:'方案执行 · 代码输出', reporter:'成果汇总'};
const apps = {codex:{name:'Codex',icon:'/assets/codex.ico'},dsh:{name:'DeepSeek Harness',icon:'/assets/dsh.svg'},antigravity:{name:'Antigravity',icon:'/assets/antigravity.svg'}};
const statuses = {todo:['待派发',''],queued:['等待应用领取','blue'],running:['进行中','green'],blocked:['受议题影响','amber'],review:['待核验','blue'],done:['已验收','green'],cancelled:['已撤销','']};
let state;
const query = new URLSearchParams(location.search);
const ui = {
  page:['overview','members','issues','deliveries','settings'].includes(query.get('page')) ? query.get('page') : 'overview', project:query.get('project') || localStorage.getItem('relay.project'),
  issue:query.get('issue') || localStorage.getItem('relay.issue') || null,
  chatMode:query.get('mode') || localStorage.getItem('relay.chatMode') || 'issue',
  chat:query.get('chat') === 'popout' || localStorage.getItem('relay.chat') === '1', standalone:query.get('chat') === 'popout',
  choices:{}, drafts:{}, files:{}, cleanupPlan:null, offline:false, polling:false, submitting:false, composing:false, pendingRender:false, execOpen:false,
};
const current = () => state?.projects.find(p => p.id === ui.project) || state?.projects.find(p => !p.demo) || state?.projects[0];
const member = id => state.agents.find(a => a.id === id);
const name = id => id === 'user' ? '我' : (member(id)?.name || id);
const currentIssue = () => current()?.issues.find(i => i.id === ui.issue);
const toDate = value => {
  if (value === null || value === undefined || value === '') return null;
  const parsed = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
};
const date = value => {const parsed = toDate(value);return parsed ? new Intl.DateTimeFormat('zh-CN', {hour:'2-digit',minute:'2-digit',hour12:false}).format(parsed) : '时间未知';};
const bytes = n => n >= 1073741824 ? `${(n/1073741824).toFixed(1)} GB` : n >= 1048576 ? `${(n/1048576).toFixed(1)} MB` : n >= 1024 ? `${(n/1024).toFixed(1)} KB` : `${n} B`;
const datetime = value => {const parsed = toDate(value);return parsed ? new Intl.DateTimeFormat('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(parsed) : '时间未知';};
const appIcon = (id, small = false) => id === 'user' ? '<span class="avatar-user">我</span>' : `<span class="app-icon${small ? ' small' : ''}"><img src="${apps[member(id)?.app]?.icon || apps.codex.icon}" alt="${esc(apps[member(id)?.app]?.name || '')} 图标"></span>`;
const btn = (action, label, ico = '', css = '', attrs = '') => `<button type="button" class="btn ${css}" data-action="${action}" ${attrs}>${ico ? icon(ico) : ''}${label}</button>`;
const badge = (label, tone = '', ico = '') => `<span class="badge ${tone}">${ico ? icon(ico) : ''}${esc(label)}</span>`;
const chip = id => `<span class="member-chip">${appIcon(id, true)}${id === 'user' ? '' : esc(name(id))}</span>`;
const agentOptions = selected => state.agents.map(a => `<option value="${a.id}" ${a.id === selected ? 'selected' : ''}>${esc(a.name)}</option>`).join('');

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || '请求失败，请稍后重试。');
  return body;
}
async function mutate(action, args = {}) {
  const body = await request('/api/action', {method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({action,args:{project_id:current()?.id,...args}})});
  const runtime = state?.runtime;
  const codexNative = state?.codexNative;
  state = body.state;
  state.runtime = runtime;
  state.codexNative = codexNative;
  render();
  return body.result;
}
function toast(message, error = false) {
  const el = document.createElement('div');
  el.className = `toast${error ? ' error' : ''}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), error ? 6500 : 4200);
}

function presence(agent) {
  const host=state?.runtime?.hosts?.find(h=>h.agent===agent.id && h.online);
  if(host) return `<span class="presence"><i class="dot"></i>原生连接在线</span>`;
  const native=state?.codexNative?.find(h=>h.agent===agent.id);
  if(native?.connectionStatus==='connecting') return `<span class="presence"><i class="dot off"></i>正在后台连接</span>`;
  if(native?.connectionStatus==='failed') return `<span class="presence"><i class="dot off"></i>接入需要处理</span>`;
  if (!agent.last_seen) return `<span class="presence"><i class="dot off"></i>${agent.setup?.configured ? '等待应用确认' : '尚未接入'}</span>`;
  const recent = Date.now() - Date.parse(agent.last_seen) < 65000;
  return `<span class="presence" title="最近调用 ${esc(datetime(agent.last_seen))}"><i class="dot${recent ? '' : ' off'}"></i>${recent ? '最近有响应' : `上次响应 ${esc(date(agent.last_seen))}`}</span>`;
}
function roleStatus(p, role) {
  if (p.paused) return badge('已暂停派发','','pause');
  if (role === 'planner' && p.tasks.some(t=>t.status==='review')) {const agent=p.assignments.planner;const bound=state.runtime?.bindings?.find(b=>b.project===p.id && b.agent===agent);const host=state.runtime?.hosts?.find(h=>h.agent===agent && h.online);const native=state.codexNative?.find(h=>h.agent===agent);if(!bound || !host?.sessions?.some(s=>s.id===bound.session) || native?.unavailable?.[bound.session]) return badge('审核等待连接','amber','clock');}
  if (role === 'planner') return p.issues.some(i => i.status === 'open') ? badge('待评估议题','green','chat') : p.tasks.some(t => t.status === 'review') ? badge('待核验成果','blue','file') : badge('等待进展','','clock');
  if (role === 'reporter') return p.summary ? badge('已提交汇总','green','check') : p.summary_requested ? badge('汇总等待领取','blue','clock') : badge('等待交付材料','','circle');
  const owned = p.tasks.filter(t => t.owner === p.assignments.executor);
  if (owned.some(t => t.status === 'blocked')) return badge('等待方案决定','amber','clock');
  if (owned.some(t => t.status === 'running')) return badge('正在执行','green','circle');
  if (owned.some(t => t.status === 'queued')) return badge('等待应用领取','blue','clock');
  if (owned.length && owned.every(t => t.status === 'done')) return badge('已完成执行','green','check');
  return badge('等待任务派发','','circle');
}
function nativeInfo(p,a) {
  const binding=state.runtime?.bindings?.find(b=>b.project===p.id && b.agent===a.id);
  const host=state.runtime?.hosts?.find(h=>h.agent===a.id);
  const session=(host?.sessions || []).find(s=>s.id===binding?.session);
  const lastEnd=state.runtime?.activity?.find(e=>e.agent===a.id && e.session===binding?.session && e.type==='turn/end');
  const fault=lastEnd?.data?.reason?.kind==='error' && session?.status!=='running' ? lastEnd.data.reason.error : null;
  const otherRunning=(host?.sessions || []).filter(s=>s.id!==binding?.session && s.status==='running' && s.workspace && p.workspace && s.workspace.toLowerCase()===p.workspace.toLowerCase()) || [];
  const codex=state.codexNative?.find(h=>h.agent===a.id);
  const connecting=codex?.connectionStatus==='connecting';
  const connectionError=codex?.connectionError || codex?.unavailable?.[binding?.session] || codex?.error;
  const problem=connectionError ? (/active writer/i.test(connectionError) ? '这个 Codex thread 已被另一个写入者持有：通常是桌面端正在运行同一会话。面板不会去删锁或关共享应用；断开本面板的 Codex 连接即可停止争用，之后可改用独立会话重连。' : 'Codex 暂未连接成功，审核材料已保留，可以重试或绑定其他会话。') : '';
  // The holder's identity is not part of the app-server protocol, so it is
  // reported as unknown rather than inferred. What *is* known is stated: the
  // exact thread, that this panel is the one contending, and where the error
  // came from.
  const writerDetail=connectionError && /active writer/i.test(connectionError)
    ? `<p>线程：<code>${esc(binding?.session || '未绑定')}</code></p><p>冲突来源：本面板的后台 Codex 与该 thread 的现有写入者（通常是桌面端会话）互相争用。</p><p>持有者：<strong>未知</strong> —— Codex app-server 协议不返回持有者标识，无法从面板侧确认是哪一方；本面板已将自己这一侧列为可释放对象。</p><p>可执行：先「断开本面板 Codex 连接」停止争用，再「使用独立审核会话」新建不与他人共用的会话。</p>`
    : '';
  const modelLabel=a.app==='codex' ? '原生配置模型：' : '原生模型：';
  const boundLine=binding ? `<div class="bound-session"><span class="small muted">绑定会话</span><code>${esc(binding.session)}</code>${badge(`轮次 ${binding.generation || 1}`)}${session ? badge(session.status === 'running' ? '执行中' : '空闲', session.status === 'running' ? 'green' : '') : badge('会话未在线', 'amber')}</div>` : '<div class="bound-session small muted">尚未绑定执行会话</div>';
  return `${boundLine}<span class="small muted">${a.app==='antigravity' ? '原生唤醒尚未支持' : session ? (!host.online ? '原应用离线 · ' : '')+modelLabel+esc(session.model || '尚未确认')+(session.nextModel ? ' · 下次执行：'+esc(session.nextModel) : '') : host?.online ? '原生连接已就绪，请创建或绑定工作会话' : '原生会话未连接'}</span>${a.app==='codex' ? `<span class="small muted">独立 Codex 原生后台会话</span>${!codex?.connected || connectionError ? btn('native-connect',connecting ? '正在连接…' : '连接 / 重试 Codex','','ghost',`data-agent="${a.id}" ${connecting ? 'disabled' : ''}`) : ''}${connectionError?.includes('重新授权') ? btn('connect','更新接入配置','','ghost',`data-agent="${a.id}"`) : ''}${connecting ? '<span class="small muted">后台正在连接，其他操作可继续。</span>' : ''}${problem ? `<span class="native-error">${esc(problem)}</span><details><summary>技术详情</summary>${writerDetail}<pre>${esc(connectionError)}</pre></details>` : ''}${connectionError && /active writer/i.test(connectionError) ? btn('native-release','断开本面板 Codex 连接','','ghost',`data-agent="${a.id}"`) : ''}${(codex?.approvals || []).map(r=>btn('native-approval','原生会话需要你确认','','',`data-agent="${a.id}" data-request="${esc(r.key)}"`)).join('')}` : ''}${codex?.unavailable?.[binding?.session] ? btn('independent-native','使用独立审核会话','','ghost',`data-agent="${a.id}"`) : ''}${fault ? `<details class="small muted"><summary>上次执行记录 · ${esc(datetime(lastEnd.created || lastEnd.at || ''))}</summary><p>${/usage limit/i.test(fault.message || '') ? '上次执行遇到用量限制；这不是连接故障，当前额度需由原应用确认。' : '此前一次执行未完成，记录供排查参考。'}</p><pre>${esc(fault.message || '请在原应用查看记录')}</pre></details>` : ''}${a.app==='dsh' && binding && otherRunning.length ? `<span class="small muted">另有 ${otherRunning.length} 个同工作区会话正在运行但未绑定本职责；面板只向已绑定会话 ${esc(binding.session)} 派发。</span>` : ''}${binding ? `<div class="row">${['dsh','codex'].includes(a.app) ? btn('native-model','选择模型','','ghost',`data-agent="${a.id}"`) : ''}${btn('native-activity','原生活动','','ghost',`data-agent="${a.id}"`)}</div>` : ''}`;
}
async function releaseNativeCodex(agentId) {
  openModal('断开本面板的 Codex 连接',`<p class="modal-lead">只释放面板自己持有的后台 Codex 连接，用来停止与桌面端争用同一个 thread。不会删除会话、不会改接入配置、不会关闭桌面端 Codex，也不动任何锁文件。断开后可随时重连。</p>${formActions('断开本面板连接')}`,async()=>{
    const result=await request('/api/native/codex/release',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({agent_id:agentId})});
    closeModal();state=await request('/api/state');render();toast(result.notice || '已断开本面板连接');
  });
}
function independentNative(agentId) {
  openModal('使用独立审核会话',`<p class="modal-lead">为本项目新建独立的原生工作会话，避开旧会话的占用。原聊天和成果保留，旧会话不被关闭或归档。新会话在你恢复派发后读取任务组材料继续工作，不重放旧任务。</p>${formActions('创建并绑定独立会话')}`,async()=>{
    await request('/api/runtime/create',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:current().id,agent_id:agentId,replace:true})});
    closeModal();toast('正在准备独立会话，任务暂停状态保持不变。');
  });
}
async function connectNativeCodex(agentId) {
  if(!member(agentId)?.setup?.configured) return connect(agentId);
  openModal('连接本机 Codex 原生 Agent',`<p class="modal-lead">使用已安装的 Codex 自带原生 harness，在后台建立独立工作会话，沿用本机配置、登录和可用插件。它与当前桌面对话分别保存。原生权限请求会在面板中交给你确认。</p>${formActions('连接原生 Codex')}`,async()=>{
    await request('/api/native/codex/connect',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({agent_id:agentId})});closeModal();state=await request('/api/state');render();toast('已在后台尝试连接，面板仍可继续使用。');
  });
}
function nativeApproval(agentId,key) {
  const native=state.codexNative?.find(h=>h.agent===agentId)?.approvals.find(r=>r.key===key);
  if(!native) return toast('原生请求已失效，请刷新后查看。',true);
  const p=native.params;
  if(p.mode==='openai/userVerification') return openModal('需要原应用设备验证',`<p>${esc(p.description || p.message || '此请求需要设备验证，面板无法代替。')}</p>${btn('native-decline','拒绝此次请求','','',`data-agent="${agentId}" data-request="${esc(key)}"`)}`);
  const questions=p.questions || [];
  const properties=Object.entries(p.requestedSchema?.properties || {});
  let fields=questions.map((q,i)=>field(q.question || q.header,`${q.options?.length ? `<select class="input" name="answer_${i}">${q.options.map(o=>`<option value="${esc(o.label)}">${esc(o.label)}${o.description ? ' · '+esc(o.description) : ''}</option>`).join('')}</select>` : input('answer_'+i,'请输入回答','','required')}`)).join('');
  fields+=properties.map(([name,s],i)=>field(s.title || name,s.enum ? `<select class="input" name="content_${i}">${s.enum.map((v,n)=>`<option value="${n}">${esc(s.enumNames?.[n] || v)}</option>`).join('')}</select>` : s.type==='boolean' ? `<select class="input" name="content_${i}"><option value="false">否</option><option value="true">是</option></select>` : input('content_'+i,s.description || '', '',p.requestedSchema.required?.includes(name) ? 'required' : ''))).join('');
  const details=p.command || (p.permissions ? JSON.stringify(p.permissions,null,2) : '');
  const url=p.mode==='url' && /^https?:\/\//.test(p.url || '') ? `<a href="${esc(p.url)}" target="_blank" rel="noreferrer">打开原生请求的授权页面</a>` : '';
  openModal('Codex 原生会话请求',`<p class="modal-lead">${esc(p.reason || p.message || p.description || '请确认本次原生操作。')}</p>${details ? `<pre class="deliverable-evidence">${esc(details)}</pre>` : ''}${url}${fields}<p class="small muted">来自 ${esc(name(agentId))} 的原生会话；面板不会自动批准。</p>${btn('native-decline','拒绝','','ghost',`data-agent="${agentId}" data-request="${esc(key)}"`)}${formActions(questions.length ? '提交回答' : '允许本次请求')}`,async form=>{
    const values=new FormData(form);
    const answers=Object.fromEntries(questions.map((q,i)=>[q.id,values.get('answer_'+i)]));
    const content=Object.fromEntries(properties.map(([key,s],i)=>{const v=values.get('content_'+i);return [key,s.enum ? s.enum[Number(v)] : s.type==='boolean' ? v==='true' : ['number','integer'].includes(s.type) ? Number(v) : v];}));
    await nativeRespond(agentId,key,{decision:'accept',answers,content});
  });
}
async function nativeRespond(agentId,key,answer) {
  await request('/api/native/codex/respond',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({agent_id:agentId,request_key:key,answer})});closeModal();state=await request('/api/state');render();toast('已将你的决定交回原生会话。');
}
async function nativeCommand(agentId,operation,selection) {
  const job=await request('/api/runtime/model',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:current().id,agent_id:agentId,operation,selection})});
  for(let i=0;i<35;i++) {
    await new Promise(resolve=>setTimeout(resolve,800));
    const next=await request('/api/state');state=next;
    const delivered=next.runtime.jobs.find(j=>j.id===job.id);
    if(delivered?.status==='failed') throw new Error(delivered.detail.receipt || '原应用拒绝了操作');
    if(delivered?.status==='delivered') return delivered.detail.result;
    if(delivered?.status==='uncertain') break;
  }
  throw new Error('等待原应用回执超时，请在原生活动中查看结果。不会自动重复发送。');
}
function effortField(models,index) {
  const model=models[Number(index)];
  if(!model?.efforts?.length) return '';
  return `<label class="field-label" for="native-effort">思考深度</label><select class="input" id="native-effort" name="native_effort">${model.efforts.map(e=>`<option value="${esc(e)}"${e===model.effortDefault?' selected':''}>${esc(e)}${e===model.effortDefault?'（默认）':''}</option>`).join('')}</select>`;
}
async function chooseNativeModel(agentId) {
  toast('正在从原应用读取可用模型…');
  const result=await nativeCommand(agentId,'model-catalog');
  if(!result?.models?.length) throw new Error('原应用没有返回可用模型。');
  const models=result.models;
  // An effort list comes from the same route info the app validates a request
  // against, so only supported depths appear; a route without reasoning support
  // shows no selector rather than a choice that would be refused later.
  openModal('选择原生会话使用的模型',`<p class="modal-lead">模型列表来自原应用。只更改此绑定会话的模型与思考深度；应用的全局默认设置保留。正在执行时会等待会话空闲。</p><select class="input" name="native_model">${models.map((m,index)=>`<option value="${index}">${esc(m.label)} · ${esc(m.model)}</option>`).join('')}</select><div id="native-effort-slot">${effortField(models,0)}</div>${formActions('设置此会话模型')}`,async form=>{
    const selected=models[Number(new FormData(form).get('native_model'))];
    if(!selected) throw new Error('请选择模型');
    const effort=new FormData(form).get('native_effort');
    const payload=effort ? {...selected,reasoningEffort:effort} : selected;
    await nativeCommand(agentId,'model-select',payload);closeModal();render();toast('原应用已确认：下次请求使用 '+selected.model+(effort?' · 深度 '+effort:''));
  });
  const modelSelect=document.querySelector('.modal select[name="native_model"]');
  const slot=document.getElementById('native-effort-slot');
  if(modelSelect && slot) modelSelect.addEventListener('change',()=>{slot.innerHTML=effortField(models,modelSelect.value);});
}
function nativeActivity(agentId) {
  const binding=state.runtime?.bindings?.find(b=>b.project===current().id && b.agent===agentId);
  const events=state.runtime?.activity?.filter(e=>e.agent===agentId && e.session===binding?.session) || [];
  const jobs=state.runtime?.jobs?.filter(j=>j.project===current().id && j.agent===agentId) || [];
  openModal('原生 Agent 执行记录',`<p class="modal-lead">本项目的执行会话：<code>${esc(binding?.session || '未绑定')}</code>${binding?.generation ? `（绑定轮次 ${binding.generation}）` : ''}。这是面板实际派发的原生会话，可能不是你当前正在看的那个窗口；送达回执只表示原应用收到任务，工具活动和交付依据来自实际执行。</p><div class="native-events">${events.map(e=>`<article><strong>${esc(e.type)}</strong><time>${esc(date(e.created))}</time><pre>${esc(e.data?.name || e.data?.model || e.data?.text || JSON.stringify(e.data || {}))}</pre></article>`).join('') || '<p>尚未收到原生活动。</p>'}<h3>任务与控制回执</h3>${jobs.map(j=>`<article><strong>${esc(j.detail.kind)} · ${esc(j.status)}</strong><pre>${esc(j.detail.receipt || '等待原应用确认')}</pre></article>`).join('')}</div>`);
}
function bindNative(agentId) {
  if(!['dsh','codex'].includes(member(agentId)?.app)) return toast('此应用当前仅支持协作工具信箱，原生自动唤醒尚未完成。',true);
  const host=state.runtime?.hosts?.find(h=>h.agent===agentId);
  if(!host?.online) return toast('原生连接尚未就绪。请先授权接入原应用，并让应用加载原生连接插件。',true);
  const sessions=host.sessions;
  const bound=state.runtime?.bindings?.find(b=>b.project===current().id && b.agent===agentId)?.session;
  openModal('绑定原生 Agent 会话',`<p class="modal-lead">请选择该项目对应的原生会话。会话工作区必须与项目一致；模型与插件由原生 harness 提供。已绑定的会话会收到面板派发；其他同工作区窗口仍可各自运行。</p><select class="input" name="native_session">${sessions.map(s=>`<option value="${esc(s.id)}" ${s.id===bound ? 'selected' : ''}>${s.id===bound ? '[本项目已绑定] ' : ''}${s.status==='running' ? '[运行中] ' : ''}${esc(s.title)} · ${esc(s.id)} · ${esc(s.model || '模型未确认')} · ${esc(s.workspace)}</option>`).join('')}</select>${btn('create-native','创建此项目的原生工作会话','plus','',`data-agent="${agentId}"`)}${formActions('确认绑定')}`,async form=>{
    await request('/api/runtime/bind',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:current().id,agent_id:agentId,session_id:new FormData(form).get('native_session')})});
    state=await request('/api/state');closeModal();render();toast('已绑定真实会话，后续派发由原应用接收。');
  });
}
function cards(p) {
  return `<div class="member-grid">${Object.entries(roles).map(([role,label]) => {
    const a = member(p.assignments[role]);
    return `<article class="member-card"><div class="row">${appIcon(a.id)}<div class="grow"><div class="member-name" title="${esc(a.name)}">${esc(a.name)}</div><div class="member-role">${label}</div></div></div><div class="app-caption">工作应用：${esc(apps[a.app].name)}</div>${roleStatus(p,role)}<div class="native-binding">${nativeInfo(p,a)}${btn('bind-native','绑定原生会话','link','ghost',`data-agent="${a.id}"`)}</div><div class="member-footer">${presence(a)}${btn('connect','接入','link','ghost',`data-agent="${a.id}"`)}</div></article>`;
  }).join('')}</div>`;
}
function progress(p) {
  let stage = p.summary ? 3 : p.tasks.length && p.tasks.every(t => t.status === 'done') ? 3 : p.tasks.length && p.tasks.every(t => ['review','done'].includes(t.status)) ? 2 : p.tasks.some(t => t.dispatched) ? 1 : 0;
  return `<div class="progress" aria-label="任务阶段">${['方案与分工','开发执行','结果核验','汇总交付'].map((label,i) => `${i ? '<span class="line"></span>' : ''}<span class="step ${i < stage ? 'complete' : i === stage ? 'current' : ''}"><span class="step-icon">${i < stage ? icon('check') : ''}</span>${label}</span>`).join('')}</div>`;
}
function tasks(p) {
  const visible=p.tasks.filter(t=>!t.archived);
  return `<div class="task-table"><table><thead><tr><th>工作板块</th><th>负责人</th><th>状态</th><th>管理</th></tr></thead><tbody>${visible.map((t,i) => `<tr><td><div class="task-title">${icon(i % 2 ? 'screen' : 'layers')}<button data-action="task-detail" data-task="${t.id}">${esc(t.title)}</button></div></td><td class="owner-cell">${esc(name(t.owner))}</td><td>${badge(...statuses[t.status], t.status === 'blocked' ? 'clock' : t.status === 'done' ? 'check' : '')}</td><td>${btn('record-archive','回收','close','ghost',`data-kind="task" data-item="${t.id}"`)}</td></tr>`).join('')}</tbody></table>${!visible.length ? '<div class="table-empty">暂无可见工作板块，可从回收区恢复。</div>' : ''}</div><div class="table-actions">${p.closed ? '' : btn('new-task','添加板块','plus','ghost')}<span class="hint">执行结果提交后，由监督者核验</span>${p.closed ? badge('任务已结束，不再派发') : !p.demo ? btn('dispatch','派发任务','play','',p.paused || !visible.some(t => !t.dispatched) ? 'disabled' : '') : badge('演示数据，不派发到应用')}${btn('record-bin','记录回收区','layers','ghost')}</div>`;
}
// Unread mentions of the user. The server counts them per issue (and per project
// chat as "project"); the panel draws a red dot and clears a mention only when
// that message has actually been on screen.
const mentionCount = (p, key) => (p?.mention_counts?.[key] || 0);
const activeProject = () => (state?.projects || []).find(x => x.id === ui.project) || (state?.projects || [])[0] || null;
function issueCard(i, full = false, project) {
  const unread = mentionCount(project || activeProject(), i.id);
  const dot = unread ? `<b class="mention-dot" data-action="open-issue-mention" data-issue="${i.id}" title="有 ${unread} 条提到你的未读消息">${unread}</b>` : '';
  return `<article class="issue-card ${unread ? 'has-mention' : ''}"><div class="row between"><button class="issue-title" data-action="open-issue" data-issue="${i.id}">${esc(i.title)}</button>${dot}${badge(i.status === 'open' ? (i.options.length ? '待决定' : '讨论中') : '已解决', i.status === 'open' ? 'amber' : 'green')}</div><div class="issue-meta">发起：${esc(name(i.creator))} <span aria-hidden="true">·</span> 群主：我${full ? ` · ${esc(datetime(i.created))}` : ''}</div><div class="issue-bottom"><div class="member-chips">${chip('user')}${i.members.map(chip).join('')}${btn('invite','邀请成员','plus','ghost',`data-issue="${i.id}"`)}</div>${btn('open-issue',`打开群聊${full ? ` · ${i.messages.filter(m=>!m.archived).length}` : ''}`,'chat','',`data-issue="${i.id}"`)}${btn('record-archive','回收议题','close','ghost',`data-kind="issue" data-item="${i.id}"`)}</div></article>`;
}
function overview(p) {
  const openProject = (p.chat?.length ? `项目群聊 · ${p.chat.length}` : '项目群聊') + (mentionCount(p,'project') ? ` · @${mentionCount(p,'project')} 未读` : '');
  return `<div class="dashboard"><section class="workspace"><div class="section-heading"><div><h1>任务与分工</h1><p>指定负责人，让协作在原应用中发生</p></div>${p.closed ? '' : btn('assign','调整分工','edit')}</div>${cards(p)}${progress(p)}${tasks(p)}<div class="section-divider"></div><div class="section-heading"><h2>关键议题</h2><div class="row">${btn('open-project-chat',openProject,'chat','ghost')}${p.closed ? '' : btn('new-issue','发起议题','plus')}</div></div><div class="issue-list">${p.issues.some(i=>!i.archived) ? p.issues.filter(i=>!i.archived).slice().sort((a,b) => (a.status === 'resolved') - (b.status === 'resolved')).map(i => issueCard(i)).join('') : `<div class="empty">${icon('chat')}还没有议题。<br>遇到阻碍、分歧或新想法时，在这里一起讨论；没有议题也可以在项目群聊里交接。<div class="empty-actions">${p.closed ? '' : btn('new-issue','发起议题','plus')}${btn('open-project-chat','打开项目群聊','chat','ghost')}</div></div>`}</div><div class="project-goal"><strong>任务目标</strong><br>${esc(p.goal)}${!p.demo ? `<div class="workspace-binding">${icon('folder')}<code>${esc(p.workspace || '尚未指定工作区')}</code>${!p.workspace ? btn('set-workspace','指定工作区','folder','ghost') : ''}</div>` : ''}</div></section>${p.closed ? closedRail(p) : decisions(p)}</div>`;
}
function decisions(p) {
  const pending = p.issues.filter(i => !i.archived && i.status === 'open' && i.options.length);
  const open = p.issues.filter(i => !i.archived && i.status === 'open');
  const activeTasks=p.tasks.filter(t=>!t.archived);
  const allDone = activeTasks.length > 0 && activeTasks.every(t => t.status === 'done');
  const ready = allDone && !open.length;
  return `<aside class="decision-rail" aria-label="用户决策与汇总"><section><h2 class="rail-heading">需要我决定 ${pending.length ? `<span class="number-badge">${pending.length}</span>` : ''}</h2>${pending.map(i => {
    const choice = ui.choices[i.id] || i.options[0].id;
    const index = i.options.findIndex(o => o.id === choice);
    const affected = p.tasks.filter(t => i.task_ids.includes(t.id));
    return `<article class="decision-card"><h3>调整实现方案</h3><div class="decision-context">关联议题：${esc(i.title)}</div>${i.options.map((o,j) => `<label class="option"><input type="radio" name="option-${i.id}" value="${o.id}" data-choice="${i.id}" ${o.id === choice ? 'checked' : ''}><span><span class="option-title">${String.fromCharCode(65+j)} &nbsp; ${esc(o.title)}</span><span class="option-detail">${esc(o.detail)}</span></span></label>`).join('')}<div class="decision-impact">${affected.length ? `影响：${esc(affected.map(t => t.title).join('、'))}暂缓，其他板块可继续` : '此议题未暂停工作板块'}</div>${btn('resolve',`确认 ${String.fromCharCode(65+index)} · 继续任务`,'play','primary full',`data-issue="${i.id}"`)}<div class="small-actions">${btn('supplement','补充要求','','',`data-issue="${i.id}"`)}${btn('open-issue','查看讨论依据','arrow','ghost',`data-issue="${i.id}"`)}</div></article>`;
  }).join('') || `<div class="no-decision">${icon('check')}暂时没有需要你决定的事项<br>成员有分歧时会在这里提出</div>`}</section><section>${(p.stages || []).length ? `<section class="rail-section"><h2>阶段汇总</h2>${p.stages.slice(-4).reverse().map(s => `<div class="reporter-row"><span>${esc(s.title || '已验收板块')} · 第 ${s.revision} 版 ${s.status === 'submitted' ? badge('已提交', 'green', 'check') : badge('等待汇总', 'blue', 'clock')}</span>${s.status === 'submitted' ? btn('stage-view','正文','file','ghost',`data-stage="${s.id}"`) : ''}</div>`).join('')}${btn('stage-list',`全部阶段记录 · ${p.stages.length}`,'layers','ghost full')}</section>` : ''}<section class="rail-section"><h2>最终汇总</h2><div class="reporter-row"><span>负责人 &nbsp; <strong>${esc(name(p.assignments.reporter))}</strong></span>${btn('assign','更换','','')}</div><div class="checklist">${[['方案与决策记录',p.tasks.length > 0 && !open.length],['文件改动与检查结果',allDone],['未完成事项',!!p.summary]].map(([label,checked]) => `<div class="checklist-item"><span class="check-box${checked ? ' checked' : ''}">${checked ? icon('check') : ''}</span>${label}</div>`).join('')}<div class="checklist-foot">${p.summary ? '负责人已提交汇总，可查看交付记录' : ready ? '核验材料已齐全，可以派发汇总' : '材料齐全后，再交给负责人汇总'}</div></div><div class="stack rail-buttons">${p.summary ? btn('deliveries','查看交付说明','file','full') : btn('summary-request',p.summary_requested ? '汇总已派发 · 等待响应' : '交给负责人汇总','play','full',!ready || p.summary_requested || p.demo || p.paused ? 'disabled' : '')}${btn('export','导出当前记录','download','ghost full')}</div></section><div class="principle">${icon('info')}你决定方向，成员负责推进</div></aside>`;
}
function closedRail(p) {
  const resources = (state.runtime?.resources || []).filter(r => r.project === p.id);
  const labels = {active:'未处理',archiving:'已请求原应用归档',archived:'已归档',unbound:'已解绑',retry:'待重试',failed:'失败'};
  return `<aside class="decision-rail" aria-label="任务结束"><section><h2 class="rail-heading">任务已结束</h2><div class="no-decision">${icon('check')}结束时间：${esc(p.closed_at ? datetime(p.closed_at) : '未记录')}<br>保留期 ${p.retention_days || 7} 天；群聊、决定、核验依据与交付记录保留，可随时导出。</div><div class="stack rail-buttons">${btn('cleanup-preview','清理预览','layers','full')}${btn('export','导出当前记录','download','ghost full')}</div><p class="small muted">只归档面板为此项目创建的会话；手动绑定的会话只解绑；共享应用与插件保留，不按进程名关闭。</p></section><section class="rail-section"><h2>已登记资源</h2>${resources.length ? resources.map(r => `<div class="resource-row"><span class="grow"><strong>${r.origin === 'panel-created' ? '面板创建' : '手动绑定'}</strong><br><code>${esc(r.session || '—')}</code><br><span class="small muted">${esc(labels[r.status] || r.status)}${r.detail ? ` · ${esc(r.detail)}` : ''}</span></span></div>`).join('') : `<div class="no-decision">${icon('info')}没有登记的原生会话，结束时不需要归档或解绑。</div>`}</section></aside>`;
}
function closeProject() {
  const p = current();
  if (p.closed) return toast('任务已经结束。');
  openModal('结束任务',`<p class="modal-lead">结束后停止派发并撤销未完成的工作板块。已提交的汇报、核验依据、决定与群聊记录保留，可以继续查看和导出。</p><div class="permissions"><div class="permission">${icon('tasks')}<span>只归档面板为此项目创建的原生会话</span></div><div class="permission">${icon('link')}<span>手动绑定的会话只解除本项目绑定</span></div><div class="permission">${icon('info')}<span>共享应用、插件与接入配置保留，不关闭进程</span></div></div>${field('清理保留期（天）',input('retention_days','7',String(p.retention_days || 7),'type="number" min="1" max="365" required'))}<label class="checkbox-label"><input type="checkbox" name="confirm" required>我确认结束此任务，并按登记处理会话</label>${formActions('确认结束')}`,async form => {
    const values = new FormData(form);
    const runtime = state.runtime, codexNative = state.codexNative;
    const body = await request('/api/project/close',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:p.id,retention_days:Number(values.get('retention_days'))})});
    state = body.state; state.runtime = runtime; state.codexNative = codexNative;
    closeModal(); render();
    const plan = body.plan || {};
    toast(`任务已结束：撤销 ${plan.cancelled || 0} 个待执行任务；归档请求 ${(plan.archived || []).length} 个、解绑 ${(plan.unbound || []).length} 个、待重试 ${(plan.retry || []).length} 个。`);
  });
}
async function cleanupPreview() {
  const p = current();
  const data = await request(`/api/cleanup/preview?project=${encodeURIComponent(p.id)}`);
  ui.cleanupPlan = data.id;
  const list = (items, empty) => items.length ? items.map(i => `<div class="cleanup-item"><span class="grow">${esc(i.kind || '保留')}<br><code>${esc(i.path)}</code><span class="small muted">${esc(i.reason)}</span></span><span class="cleanup-size">${i.size == null ? '不统计' : bytes(i.size)}</span></div>`).join('') : `<div class="no-decision">${empty}</div>`;
  const recoveries = await request(`/api/cleanup/recoveries?project=${encodeURIComponent(p.id)}`);
  const history = recoveries.length ? `<h3>可恢复区（${recoveries.length}）</h3><div class="cleanup-list">${recoveries.map(r => `<div class="cleanup-item"><span class="grow"><code>${esc(r.plan_id)}</code><span class="small muted">已移动 ${r.moved} 项 · 已恢复 ${r.restored} 项</span></span>${r.moved>r.restored ? btn('cleanup-restore','恢复','arrow','ghost',`data-plan="${esc(r.plan_id)}"`) : badge('已恢复')}</div>`).join('')}</div>` : '';
  openModal('清理预览',`<p class="modal-lead">保留期 ${data.retention_days} 天。候选只来自登记与固定规则；执行时移动到可恢复区并写清单，不删除数据。确认清单与预览不一致时会拒绝执行。</p><h3>候选（${data.items.length}）</h3><div class="cleanup-list">${list(data.items,'没有符合条件的清理候选。')}</div><h3>保留项（${data.retained.length}）</h3><div class="cleanup-list">${list(data.retained,'没有需要列出的保留项。')}</div><p class="small muted">合计：移动 ${data.totals.move_items} 项 / ${bytes(data.totals.move_bytes)}；保留 ${data.totals.retained_items} 项 / ${bytes(data.totals.retained_bytes)}。</p>${data.items.length ? `<button type="button" class="btn primary full" data-action="cleanup-execute">${icon('layers')}移入可恢复区</button>` : ''}${history}`);
}
async function cleanupExecute() {
  const p = current();
  const result = await request('/api/cleanup/execute',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:p.id,confirm:true,plan_id:ui.cleanupPlan})});
  closeModal();
  toast(`已移动 ${result.moved.length} 项到可恢复区${result.failed.length ? `；${result.failed.length} 项失败，可再次预览后重试` : ''}。`);
}
async function cleanupRestore(planId) {
  const result = await request('/api/cleanup/restore',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:current().id,plan_id:planId,confirm:true})});
  closeModal();
  toast(`已恢复 ${result.restored.length} 项${result.skipped.length ? `；${result.skipped.length} 项未恢复（原位置已有同名文件或已不存在）` : ''}。`);
}
function memberPage() {
  return `<section class="page"><div class="section-heading"><div><h1>成员与分工</h1><p>每个成员绑定一个工作应用，用职责区分同一应用中的不同会话</p></div>${btn('new-agent','添加成员','plus')}</div><div class="member-grid">${state.agents.map(a => `<article class="member-card"><div class="row">${appIcon(a.id)}<div class="grow"><div class="member-name">${esc(a.name)}</div><div class="member-role">${esc(apps[a.app].name)}</div></div>${btn('edit-agent','编辑','edit','ghost',`data-agent="${a.id}"`)}</div><div>${presence(a)}</div><div class="small muted">${a.session ? '最近会话：'+esc(a.session) : '尚无应用会话响应'}</div>${btn('connect','连接应用会话','link','full',`data-agent="${a.id}"`)}</article>`).join('')}</div><div class="connection-note"><strong>第一版的接入方式</strong><br>点击“连接应用会话”，确认授权后由面板自动完成接入配置。应用会话可领取任务、汇报进度、发起议题并读取你的决定。面板不会自行唤醒已结束的会话，也不会代替原应用调用模型。暂停为协作通知，需要应用会话配合。</div></section>`;
}
function issuesPage(p) {
  return `<section class="page"><div class="section-heading"><div><h1>议题群聊</h1><p>围绕具体问题讨论；项目级讨论在没有议题时也能继续</p></div><div class="row">${btn('open-project-chat','项目群聊','chat','ghost')}${p.closed ? '' : btn('new-issue','发起议题','plus')}</div></div><div class="issue-page-grid">${p.issues.filter(i=>!i.archived).map(i => issueCard(i,true)).join('') || `<div class="empty">${icon('chat')}还没有议题。你或负责此任务的成员都可以发起；只是想说明进度或交接，也可以直接打开项目群聊。<div class="empty-actions">${p.closed ? '' : btn('new-issue','发起议题','plus')}${btn('open-project-chat','打开项目群聊','chat','ghost')}</div></div>`}</div></section>`;
}
function settingsPage(p) {
  const isDemo = !!p.demo;
  const rows = (ui.storage || []).map(r => `<div class="storage-row"><span class="grow"><strong>${esc(r.title || r.project)}</strong>${r.live ? '' : ' ' + badge('已移入回收区', 'amber')}<br><span class="small muted">附件 ${r.uploads_count} 个 · 临时目录 ${r.scratch_files} 个文件 · 可恢复区 ${r.recovery_items} 项${r.registered ? '' : ' · 临时目录未登记'}</span></span><span class="storage-size">${bytes(r.uploads_bytes + r.scratch_bytes + r.recovery_bytes)}</span></div>`).join('');
  const total = (ui.storage || []).reduce((sum, r) => sum + r.uploads_bytes + r.scratch_bytes + r.recovery_bytes, 0);
  return `<section class="page"><div class="section-heading"><div><h1>设置与占用</h1><p>只看本应用自己创建的对象：会话库、凭据、插件与工作区都不在这里处理</p></div><div class="row">${btn('settings-refresh','刷新占用','refresh','ghost')}${btn('settings-cleanup-preview','清理预览','layers')}</div></div>
  <div class="settings-grid">
    <article class="settings-card"><h2>数据目录</h2><p class="small muted">这一页读的就是下面这个目录里的 workspace.sqlite3。面板不会再让第二个进程用别的目录占用同一个端口，所以这里显示的路径就是唯一的真实数据位置。</p><div class="storage-list"><div class="storage-row"><span class="grow"><code>${esc(state?.storage_path || '未知')}</code><br><span class="small muted">当前项目 ${state?.projects?.length || 0} 个；若列表比你预期的少，先核对这个路径</span></span>${btn('settings-refresh','重新读取','refresh','ghost')}</div></div></article>
    <article class="settings-card"><h2>存储与占用</h2><p class="small muted">按项目列出本应用登记的附件、临时目录与可恢复区容量；工作区不计入、不扫描。</p><div class="storage-list">${rows || `<div class="no-decision">${ui.storage ? '还没有登记占用。创建项目后会自动登记本应用的临时目录。' : '正在读取占用…'}</div>`}</div><p class="small muted">合计 ${bytes(total)}。会话数据库与凭据永不移动，其他非本应用创建的目录一律列为未知并保留。</p><div class="stack">${btn('settings-cleanup-preview','预览可清理对象','layers','full')}</div></article>
    <article class="settings-card"><h2>项目记录</h2><p class="small muted">把项目记录移入回收区（=${isDemo ? '演示任务不可用' : '可随时移回'}）。移入回收区会先结束项目：停止派发、撤销未完成板块、按来源归档或解绑会话；用户工作区里的文件不动。</p><div class="stack">${isDemo ? badge('演示任务不能移入回收区；重置即可') : p.closed ? `${btn('forget-project','移入回收区','archive','full')}<span class="small muted">这条记录已结束，可以直接移入回收区。</span>` : `${btn('close-project','先结束任务，再移入回收区','check','full')}<span class="small muted">结束会停止派发，并把未完成板块标记为已撤销。</span>`}</div><p class="small muted">移入后仍可在下方回收区逐条移回；不会永久清除任何真实记录。</p>
    <h3 class="small">回收区${(ui.recycled || []).length ? `（${ui.recycled.length}）` : ''}</h3><div class="storage-list">${(ui.recycled || []).map(r => `<div class="storage-row"><span class="grow"><strong>${esc(r.title || r.id)}</strong><br><span class="small muted">${r.tasks} 板块 · ${r.issues} 议题 · ${r.chat} 条群聊 · 记录 ${bytes(r.size)}</span></span>${btn('restore-project','移回列表','play','ghost',`data-project="${esc(r.id)}"`)}</div>`).join('') || '<div class="no-decision">回收区为空。</div>'}</div></article>
  </div></section>`;
}
async function refreshStorage() {
  const data = await request('/api/settings/storage');
  ui.storage = data.storage; ui.recycled = data.recycled;
  render();
}
async function restoreProject(el) {
  const id = el.dataset.project;
  const result = await request('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({action:'project_restore',args:{project_id:id}})});
  state = result.state;
  ui.project = id; ui.issue = null; ui.chat = false;
  localStorage.setItem('relay.project', id); saveChat();
  await refreshStorage();
  toast(`已把「${result.result.title}」移回列表`);
}
async function settingsCleanupPreview() {
  const data = await request('/api/settings/cleanup/preview');
  ui.cleanupPlan = data.id;
  const list = (items, empty) => items.length ? items.map(i => `<div class="cleanup-item"><span class="grow">${esc(i.kind || '保留')}<br><code>${esc(i.path)}</code><span class="small muted">${esc(i.reason)}</span></span><span class="cleanup-size">${i.size == null ? '不统计' : bytes(i.size)}</span></div>`).join('') : `<div class="no-decision">${empty}</div>`;
  openModal('应用占用预览',`<p class="modal-lead">这里只列本应用创建且已无归属的对象；会话数据库、凭据、插件与工作区一律保留。执行时移动到 data/recovery/ 并写清单。</p><h3>可清理（${data.items.length}）</h3><div class="cleanup-list">${list(data.items,'没有可清理的对象。')}</div><h3>保留项（${data.retained.length}）</h3><div class="cleanup-list">${list(data.retained,'没有需要列出的保留项。')}</div><p class="small muted">合计：移动 ${data.totals.move_items} 项 / ${bytes(data.totals.move_bytes)}。</p>${data.items.length ? `<button type="button" class="btn primary full" data-action="settings-cleanup-execute">${icon('layers')}移入可恢复区</button>` : ''}`);
}
async function settingsCleanupExecute() {
  const result = await request('/api/settings/cleanup/execute',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({plan_id:ui.cleanupPlan,confirm:true})});
  closeModal();
  toast(`已移动 ${result.moved.length} 项到 ${result.root}${result.failed.length ? `；${result.failed.length} 项失败可重试` : ''}`);
}
function forgetProject() {
  const p = current();
  if (p.demo) return toast('演示任务不能删除。',true);
  if (!p.closed) return toast('请先结束任务。',true);
  openModal('把项目记录移入回收区',`<p class="modal-lead">将把「${esc(p.title)}」的整份记录从此面板移除：目标、板块、议题、群聊、核验依据与决定历史。</p><div class="permissions"><div class="permission">${icon('file')}<span>移除前先写一份可恢复文件到 data/deleted-projects/</span></div><div class="permission">${icon('folder')}<span>项目工作区里的文件不会被删除</span></div><div class="permission">${icon('info')}<span>共享应用、插件与其他项目不受影响</span></div></div><label class="checkbox-label"><input type="checkbox" name="confirm" required>我确认把这条项目记录移入回收区（可随时移回）</label>${formActions('移入回收区')}`,async form => {
    const values = new FormData(form);
    if (values.get('confirm') !== 'on') throw new Error('请勾选确认。');
    const result = await request('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({action:'project_forget',args:{project_id:p.id}})});
    state = result.state;
    const next = state.projects.find(x => !x.demo) || state.projects[0];
    ui.project = next.id; ui.issue = null; ui.chat = false; ui.page = 'overview';
    localStorage.setItem('relay.project',ui.project); saveChat();
    closeModal(); render();
    toast(`已移入回收区；可恢复文件：${result.result.recover}`);
  });
}
function deliveries(p) {
  const events = state.events.filter(e => e.project === p.id);
  return `<section class="page"><div class="section-heading"><div><h1>交付记录</h1><p>按项目归档交付说明、核验依据和决策历史</p></div>${btn('export','导出当前项目','download')}</div><div class="delivery-projects">${state.projects.map(project => `<button class="delivery-project ${project.id===p.id ? 'selected' : ''}" data-action="delivery-project" data-project="${project.id}">${icon('folder')}<span class="grow"><strong>${esc(project.title)}</strong><span class="small muted">${project.tasks.filter(t=>t.status==='done').length} / ${project.tasks.length} 个板块已验收${project.demo ? ' · 演示' : ''}</span></span>${badge(project.summary ? '已提交汇总' : '待交付',project.summary ? 'green' : '')}</button>`).join('')}</div><div class="delivery-project-heading"><h2>${esc(p.title)}</h2><p>${esc(p.workspace || (p.demo ? '演示项目' : '工作区尚未指定'))}</p></div><section class="delivery-panel"><h2>成果汇总</h2>${p.summary ? `<div class="row small muted">${appIcon(p.summary.actor,true)}${esc(name(p.summary.actor))} · ${esc(datetime(p.summary.created))}</div><pre>${esc(p.summary.body)}</pre>` : '<p class="page-lead">汇总负责人尚未提交。各工作板块核验通过、议题处理完毕后，可从任务总览派发汇总。</p>'}</section>${p.tasks.filter(t => t.evidence).map(t => `<section class="delivery-panel"><div class="row between"><h2>${esc(t.title)}</h2>${badge(...statuses[t.status])}</div><div class="small muted">成员汇报 · ${esc(name(t.owner))}</div><div class="deliverable-evidence">${esc(t.evidence)}</div><div class="small muted">核验依据</div><div class="deliverable-evidence">${esc(t.review || '尚未核验，成员自述不等于验收通过。')}</div></section>`).join('')}<h2 class="timeline-heading">任务时间线</h2><div class="history">${events.map(e => `<article class="history-event"><div class="small">${esc(datetime(e.created))} · ${esc(name(e.actor))}</div><p>${esc(e.detail.text || e.detail.name || e.kind)}</p>${e.detail.evidence ? `<p class="muted">${esc(e.detail.evidence)}</p>` : ''}</article>`).join('') || '<div class="empty">暂无记录</div>'}</div></section>`;
}
const projectMembers = p => [...new Set([...Object.values(p.assignments || {}), ...(p.tasks || []).map(t => t.owner), ...(p.issues || []).flatMap(i => i.members)])].filter(id => state.agents.some(a => a.id === id));
function currentThread(p = current()) {
  if (ui.chatMode === 'project') return {id:'project', mode:'project', title:'项目群聊', messages:p.chat || [], members:projectMembers(p), locked:!!p.closed};
  const i = currentIssue();
  if (!i || i.archived) return null;
  return {id:i.id, mode:'issue', title:i.title, messages:i.messages, members:['user', ...i.members], locked:!!p.closed, issue:i};
}
function chatLog(t) {
  const live = t.messages.filter(m => !m.archived);
  const gone = t.messages.filter(m => m.archived);
  if (!t.messages.length) return `<div class="empty">${icon('chat')}${t.mode === 'project' ? '项目群聊还没有消息。<br>可以直接说明进度、提问或交接，不必先发起议题。' : '还没有消息，先说明背景。'}</div>`;
  const body = m => `<article class="message ${m.actor === 'user' ? 'user' : ''} ${m.decision ? 'decision' : ''} ${m.archived ? 'recycled' : ''} ${m.mentions_user ? 'mentions-user' : ''}" id="msg-${m.id}" data-message="${m.id}"${m.mentions_user ? ' data-mentions-user="1"' : ''}>${appIcon(m.actor)}<div class="message-body"><div class="message-meta"><strong>${esc(name(m.actor))}</strong>${m.mentions_user ? '<span class="mention-tag" title="这条消息明确提到你">提到你</span>' : ''}<time datetime="${esc(m.created)}">${esc(date(m.created))}</time>${m.archived ? badge('已回收') : btn('record-archive','回收','','ghost',`data-kind="${t.mode === 'issue' ? 'issue_message' : 'project_message'}" data-item="${m.id}" data-issue="${t.mode === 'issue' ? t.issue.id : ''}" title="移入回收区，可恢复"`)}</div><div class="message-text">${esc(m.body)}${(m.attachments || []).map(f => `<a class="attachment" href="/files/${f.id}" download>${icon('file')}${esc(f.name)} ${icon('arrow')}</a>`).join('')}</div>${m.archived ? `<div class="small muted">${btn('record-restore','恢复','arrow','ghost',`data-kind="${t.mode === 'issue' ? 'issue_message' : 'project_message'}" data-item="${m.id}" data-issue="${t.mode === 'issue' ? t.issue.id : ''}"`)}</div>` : ''}</div></article>`;
  return (live.map(body).join('') || `<div class="empty">${icon('chat')}这个会话的消息都已回收，可在下方恢复。</div>`)
    + (gone.length ? `<details class="recycled-group"><summary>已回收消息 ${gone.length}</summary>${gone.map(body).join('')}</details>` : '');
}
function execLog(p) {
  const binding = state.runtime?.bindings?.find(b => b.project === p.id);
  if (!binding) return `<div class="empty">${icon('info')}还没有绑定执行会话。绑定后，这里显示该会话的实际原生输出。</div>`;
  const events = (state.runtime?.activity || []).filter(e => e.session === binding.session).slice().reverse();
  const jobs = (state.runtime?.jobs || []).filter(j => j.project === p.id && j.detail?.kind);
  const head = `<div class="exec-head">${badge(`执行会话 ${binding.session.slice(-12)}`)} ${badge(`绑定轮次 ${binding.generation || 1}`)}<div class="small muted">这是面板实际派发的原生会话，可能不是你正在看的窗口；换绑后只显示当前会话的活动，旧会话历史不会混入。</div></div>`;
  const turnRows = events.map(e => `<article class="message"><div class="message-body"><div class="message-meta"><strong>${esc(e.type)}</strong>${badge(`turn ${e.seq}`)}<time datetime="${esc(e.created)}">${esc(date(e.created))}</time></div><div class="message-text">${esc(e.data?.name || e.data?.model || e.data?.text || JSON.stringify(e.data || {}))}${e.data?.reason ? `<br>${esc(JSON.stringify(e.data.reason))}` : ''}</div></div></article>`).join('');
  const jobRows = jobs.map(j => `<article class="message"><div class="message-body"><div class="message-meta"><strong>${esc(j.detail.kind)}</strong>${badge(j.status)}${j.detail.generation ? badge(`代次 ${j.detail.generation}`) : ''}<time datetime="${esc(toDate(j.updated)?.toISOString() || '')}">${esc(date(j.updated))}</time></div><div class="message-text">${esc(j.detail.receipt || '等待原应用回执')}${j.detail.merged_events?.length ? `<br>合并事件：${j.detail.merged_events.join('、')}` : ''}</div></div></article>`).join('');
  return `${head}<div class="exec-section"><h4>原生工具与对话（${events.length}）</h4>${turnRows || '<p class="small muted">还没有收到原生活动。</p>'}</div><div class="exec-section"><h4>派发与回执（${jobs.length}）</h4>${jobRows || '<p class="small muted">还没有派发记录。</p>'}</div>`;
}
function chat(p = current()) {
  const t = currentThread(p);
  if (!t) return '';
  const project = t.mode === 'project';
  const draft = ui.drafts[t.id] || '';
  const files = ui.files[t.id] || [];
  const close = ui.standalone ? btn('back','返回总览','arrow','ghost') : `<button class="icon-btn" data-action="popout" title="在独立窗口打开群聊" aria-label="在独立窗口打开群聊">${icon('external')}</button><button class="icon-btn" data-action="close-chat" title="收起群聊" aria-label="收起群聊">${icon('close')}</button>`;
  const members = t.members.filter(id => id !== 'user');
  const inviteBtn = project ? '' : btn('invite','','plus','ghost',`data-issue="${t.issue.id}" aria-label="邀请成员"`);
  const note = t.locked ? '<div class="chat-note">任务已结束：群聊保留为只读记录，历史消息与决定仍可导出。</div>' : p.demo ? '<div class="chat-note">演示任务：可体验交互，不会请求模型或收到真实 AI 回复。</div>' : `<div class="chat-note">${project ? '项目群聊对所有任务成员可见；' : ''}消息进入成员信箱，应用主动读取后才能响应。Ctrl / ⌘ + Enter 发送</div>`;
  const composer = t.locked ? '' : `<div class="chat-composer"><textarea id="chat-input" data-focus="chat-input" maxlength="12000" placeholder="${project ? '向项目成员说明进度、提问或交接…' : '发消息，或 @ 指定成员…'}" aria-label="群聊消息">${esc(draft)}</textarea><div class="draft-files">${files.map((f,index) => `<span class="draft-file"><span>${esc(f.name)}</span><button data-action="remove-file" data-index="${index}" aria-label="移除附件 ${esc(f.name)}">×</button></span>`).join('')}</div><div class="composer-tools"><div class="row"><button class="icon-btn" data-action="attach" aria-label="添加附件" title="添加附件（每个不超过 5 MB）">${icon('attach')}</button><button class="icon-btn" data-action="attach-image" aria-label="添加图片" title="添加图片">${icon('image')}</button><button class="icon-btn" data-action="mention" aria-label="提及成员" title="提及成员">@</button></div><button class="send-btn" data-action="send" aria-label="发送消息" title="发送消息" ${ui.submitting ? 'disabled' : ''}>${icon('send')}</button></div></div>`;
  const meta = project ? `任务：${esc(p.title)} &nbsp; · &nbsp; 群主：我` : `发起：${esc(name(t.issue.creator))} &nbsp; · &nbsp; 群主：我`;
  const resolveBtn = !project && t.issue.status === 'open' && !t.issue.options.length ? btn('resolve-discussion','标记议题已解决','check','discussion-resolve',`data-issue="${t.issue.id}"`) : '';
  return `<aside class="chat-drawer" aria-label="${project ? '项目群聊' : '议题群聊'}"><section class="chat-panel"><header class="chat-header"><div class="chat-heading"><h2>${project ? '项目群聊' : '议题群聊'}</h2><div class="row">${close}</div></div><div class="chat-topic">${project ? '任务级讨论' : esc(t.title)}</div><div class="issue-meta">${meta}</div><div class="chat-members">${chip('user')}${members.map(chip).join('')}${inviteBtn}</div>${resolveBtn}</header><div class="chat-messages" id="chat-scroll">${chatLog(t)}</div>${note}${composer}<details class="exec-drawer" ${ui.execOpen ? 'open' : ''}><summary>执行会话输出与回执</summary>${execLog(p)}</details></section></aside>`;
}

function render() {
  if (!state) return;
  const p = current();
  if (!p) return;
  ui.project = p.id;
  const focused = document.activeElement?.dataset.focus;
  const selection = focused && !ui.composing ? [document.activeElement.selectionStart,document.activeElement.selectionEnd] : null;
  const oldChat = $('#chat-scroll');
  const scroll = oldChat ? {top:oldChat.scrollTop, bottom:oldChat.scrollHeight-oldChat.clientHeight-oldChat.scrollTop < 55} : null;
  const oldCanvas = $('.canvas')?.scrollTop || 0;
  document.body.classList.toggle('standalone',ui.standalone);
  document.body.classList.toggle('is-offline',ui.offline);
  document.title = `${p.title} · Stage Relay`;
  const openCount = p.issues.filter(i => i.status === 'open').length;
  const mentionTotal = p.mention_total || 0;
  if (ui.standalone) {
    $('#app').innerHTML = `<div class="layout">${chat(p) || '<div class="empty">找不到会话，请返回任务总览。</div>'}</div>`;
  } else {
    $('#app').innerHTML = `<div class="layout"><aside class="sidebar"><div class="brand"><img src="/assets/relay.svg" alt=""><span>Stage Relay</span></div><nav class="nav" aria-label="主导航">${[['overview','任务总览','tasks'],['members','成员与分工','users'],['issues','议题群聊','chat'],['deliveries','交付记录','file'],['settings','设置与占用','layers']].map(([page,label,ico]) => `<button data-action="nav" data-page="${page}" class="${ui.page === page ? 'active' : ''}" title="${label}" ${ui.page === page ? 'aria-current="page"' : ''}>${icon(ico)}<span>${label}</span>${page === 'issues' && (openCount || mentionTotal) ? `<b class="count${mentionTotal ? ' mention' : ''}"${mentionTotal ? ` title="${mentionTotal} 条提到你的未读消息"` : ''}>${mentionTotal ? `@${mentionTotal}` : openCount}</b>` : ''}</button>`).join('')}</nav><div class="projects"><span class="eyebrow">当前项目</span>${state.projects.map(project => `<button class="project-link ${p.id === project.id ? 'active' : ''}" data-action="project" data-project="${project.id}" title="${esc(project.title)}">${icon('folder')}<span>${esc(project.title)}</span>${project.demo ? '<small class="demo-dot">演示</small>' : ''}</button>`).join('')}</div><div class="sidebar-bottom"><button class="new-project" data-action="new-project" title="新建任务">${icon('plus')}新建任务</button><div class="local-status"><i class="dot"></i>${ui.offline ? '连接中断 · 正在重试' : '本地运行 · 自动保存'}</div></div></aside><div class="shell"><header class="topbar"><span class="topbar-title">多 Agent 协作台</span><span class="top-divider"></span><span class="project-name" title="${esc(p.title)}">项目：${esc(p.title)}</span>${p.demo ? '<button class="demo-badge demo-reset" data-action="reset-demo" title="重置演示场景">演示任务 ↺</button>' : ''}<span class="spacer"></span><span class="version">Codex + DSH · v0.3</span>${btn('open-project-chat',(p.chat?.length ? `项目群聊 ${p.chat.length}` : '项目群聊') + (mentionCount(p,'project') ? ` · @${mentionCount(p,'project')} 未读` : ''),'chat','ghost')}${p.closed ? `<span class="badge">已结束 · 只读</span>${!p.demo ? btn('forget-project','移入回收区','archive','ghost') : ''}` : `${btn('pause',p.paused ? '恢复派发' : '暂停全部',p.paused ? 'play' : 'pause')}${!p.demo ? btn('close-project','结束任务','check','ghost') : ''}`}<span class="user"><span class="avatar-user">我</span><span>我 · 群主</span></span></header>${ui.offline ? '<div class="offline-banner">暂时无法连接本地服务。页面内容为上次保存的数据，正在尝试恢复连接。</div>' : ''}<div class="main-frame"><main class="canvas">${ui.page === 'members' ? memberPage() : ui.page === 'issues' ? issuesPage(p) : ui.page === 'deliveries' ? deliveries(p) : ui.page === 'settings' ? settingsPage(p) : overview(p)}</main>${ui.chat ? chat(p) : ''}</div></div></div>`;
  }
  if ($('.canvas')) $('.canvas').scrollTop = oldCanvas;
  const newChat = $('#chat-scroll');
  if (newChat) newChat.scrollTop = !scroll || scroll.bottom ? newChat.scrollHeight : scroll.top;
  if (focused) {
    const element = $(`[data-focus="${focused}"]`);
    if (element) {element.focus({preventScroll:true}); if (selection) element.setSelectionRange(...selection);}
  }
}

const modal = $('#modal');
let modalSubmit;
function closeModal() { modal.close(); modalSubmit = null; delete modal.dataset.connectionAgent; delete modal.dataset.connectionLoading; }
function openModal(title, content, submit = null) {
  if (modal.open) modal.close();
  delete modal.dataset.connectionAgent;
  delete modal.dataset.connectionLoading;
  modal.innerHTML = `<header class="modal-head"><h2 id="modal-title">${esc(title)}</h2><button type="button" class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button></header><div class="modal-content">${submit ? '<form id="modal-form" class="form-grid">' : ''}${content}${submit ? '<div class="form-error" id="form-error" hidden></div></form>' : ''}</div>`;
  modalSubmit = submit;
  modal.showModal();
}
const formActions = (label = '保存') => `<div class="form-actions">${btn('close-modal','取消')}<button type="submit" class="btn primary">${label}</button></div>`;
const field = (label, input, help = '') => `<label class="field">${label}${input}${help ? `<span>${help}</span>` : ''}</label>`;
const input = (fieldName, placeholder = '', value = '', extra = '') => `<input class="input" name="${fieldName}" placeholder="${esc(placeholder)}" value="${esc(value)}" ${extra}>`;
const textarea = (fieldName, placeholder = '', value = '', extra = '') => `<textarea class="input" name="${fieldName}" placeholder="${esc(placeholder)}" ${extra}>${esc(value)}</textarea>`;
const selectAgent = (fieldName, selected) => `<select class="input" name="${fieldName}">${agentOptions(selected)}</select>`;
const assignmentFields = assignments => Object.entries(roles).map(([role,label]) => field(label,selectAgent(role,assignments[role]))).join('');

function workspaceField(value = '') {
  return `<label class="field">本地工作区 <div class="workspace-chooser">${input('workspace','选择文件夹或填写完整本地路径',value,'required maxlength="1000"')}${btn('choose-workspace','选择或新建','folder')}</div><span>每个任务必须绑定一个文件夹。系统选择器中也可以新建文件夹。</span></label><label class="checkbox-label"><input type="checkbox" name="create_workspace">路径不存在时，创建此文件夹</label>`;
}
async function chooseWorkspace() {
  const field=$('#modal input[name="workspace"]');
  if (window.pywebview?.api?.choose_workspace) {
    const path=await window.pywebview.api.choose_workspace();
    if(path && field?.isConnected) field.value=path;
  } else {toast('桌面版可直接选择或新建文件夹；浏览器预览请填写本地绝对路径。');field?.focus();}
}
function setWorkspace() {
  openModal('指定任务工作区',`${workspaceField()}${formActions('绑定工作区')}`,async form => {
    const f=new FormData(form);
    await mutate('workspace_set',{workspace:f.get('workspace'),create_workspace:f.has('create_workspace')});
    closeModal();toast('工作区已绑定。');
  });
}
function newProject() {
  openModal('新建任务', `<p class="modal-lead">先明确目标，再指定这次合作的负责人。创建后可以继续添加工作板块。</p>${field('任务名称',input('title','例如：制作个人项目主页','','required maxlength="100"'))}${field('任务目标',textarea('goal','希望完成什么？交付时应该满足哪些条件？','','required maxlength="6000"'))}${workspaceField()}${assignmentFields(current().demo ? {planner:"codex",executor:"deepseek",reporter:"codex"} : current().assignments)}${formActions('创建任务')}`, async form => {
    const a = Object.fromEntries(new FormData(form));
    const p = await mutate('project_create',{title:a.title,goal:a.goal,workspace:a.workspace,create_workspace:a.create_workspace==='on',assignments:{planner:a.planner,executor:a.executor,reporter:a.reporter}});
    closeModal(); chooseProject(p.id); toast('任务已创建。添加工作板块后可以派发。');
  });
}
function assign() {
  openModal('调整成员分工',`<p class="modal-lead">一个成员可以兼任多项职责。这里调整任务角色；已派发板块仍归原执行负责人。切换审核负责人会保留待审核材料；已有连接会复用，连接暂不可用时显示等待，不会重复执行原任务。</p>${assignmentFields(current().assignments)}${formActions()}`,async form => {
    const a = Object.fromEntries(new FormData(form));
    await mutate('assign',{assignments:a}); closeModal(); toast('分工已保存，并写入成员信箱。');
  });
}
function newTask(t = null) {
  openModal(t ? '编辑工作板块' : '添加工作板块',`${field('板块名称',input('title','例如：接口与数据层',t?.title,'required maxlength="150"'))}${field('负责人',selectAgent('owner',t?.owner || current().assignments.executor))}${field('验收条件',textarea('acceptance','要交付哪些文件或结果？需要哪些检查依据？',t?.acceptance,'required maxlength="3000"'))}${formActions(t ? '保存修改' : '添加板块')}`,async form => {
    const values = Object.fromEntries(new FormData(form));
    await mutate(t ? 'task_edit' : 'task_create',{...values,...(t ? {task_id:t.id} : {})}); closeModal(); toast('工作板块已保存。');
  });
}
function taskDetail(taskId) {
  const t = current().tasks.find(t => t.id === taskId);
  openModal(t.title,`<div class="task-detail"><div class="row between"><span class="row small">${appIcon(t.owner,true)}${esc(name(t.owner))}</span>${badge(...statuses[t.status])}</div><h3>验收条件</h3><p>${esc(t.acceptance)}</p><h3>成员汇报与检查结果</h3><p>${esc(t.evidence || '尚未提交。任务派发后，负责应用需要领取并汇报。')}</p><h3>核验依据</h3><p>${esc(t.review || '尚未核验')}</p>${t.review_history?.length ? `<details><summary>此前审核意见（仅供参考）</summary>${t.review_history.map(r=>`<p><strong>${esc(name(r.actor))}</strong> · ${esc(datetime(r.created))}</p><p>${esc(r.evidence)}</p>`).join('')}</details>` : ''}${t.status === 'review' ? `<div class="row wrap">${btn('accept-task','核验通过','check','primary',`data-task="${t.id}"`)}${btn('reject-task','交回返修','edit','',`data-task="${t.id}"`)}</div>` : !t.dispatched ? btn('edit-task','编辑板块','edit','',`data-task="${t.id}"`) : ''}</div>`);
}
function acceptance(taskId, approved) {
  openModal(approved ? '确认核验通过' : '交回负责人返修', `${field('核验依据',textarea('evidence',approved ? '填写你实际检查过的结果、命令或交付依据。' : '填写具体未通过项与返修要求。','','required maxlength="10000"'))}${formActions(approved ? '确认通过' : '交回返修')}`,async form => {
    await mutate('accept',{task_id:taskId,approved,evidence:new FormData(form).get('evidence')}); closeModal(); toast(approved ? '核验依据已保存。' : '返修要求已进入负责人信箱。');
  });
}
function newIssue() {
  const p = current();
  openModal('发起议题',`${field('议题名称',input('title','一个具体问题或提案','','required maxlength="150"'))}${field('说明与依据',textarea('body','发生了什么？尝试过什么？希望谁提供帮助？','','required maxlength="10000"'))}<div class="field"><div class="small">邀请成员</div><div class="check-group">${state.agents.map(a => `<label class="checkbox-label"><input type="checkbox" name="members" value="${a.id}" ${Object.values(p.assignments).includes(a.id) ? 'checked' : ''}>${esc(a.name)}</label>`).join('')}</div></div><div class="field"><div class="small">暂停受影响的板块（可不选）</div><div class="check-group">${p.tasks.filter(t => t.status !== 'done').map(t => `<label class="checkbox-label"><input type="checkbox" name="tasks" value="${t.id}">${esc(t.title)}</label>`).join('') || '<span class="small muted">还没有工作板块</span>'}</div></div><label class="checkbox-label"><input type="checkbox" id="needs-decision" name="decision">需要用户选择方案</label><div id="decision-fields" class="stack" hidden><div class="columns">${field('方案 A',input('option_a','方案名称','','maxlength="150"'))}${field('方案 B',input('option_b','方案名称','','maxlength="150"'))}</div><div class="columns">${field('方案 A 的影响',textarea('detail_a','对范围、成本或交付的影响','','maxlength="2000"'))}${field('方案 B 的影响',textarea('detail_b','对范围、成本或交付的影响','','maxlength="2000"'))}</div></div>${formActions('创建议题')}`,async form => {
    const f = new FormData(form);
    const issue = await mutate('issue_create',{title:f.get('title'),body:f.get('body'),members:f.getAll('members'),task_ids:f.getAll('tasks'),options:f.has('decision') ? [{title:f.get('option_a'),detail:f.get('detail_a')},{title:f.get('option_b'),detail:f.get('detail_b')}] : []});
    closeModal(); openIssue(issue.id); toast('议题已创建，相关成员可从信箱读取。');
  });
}
function invite(issueId) {
  const i = current().issues.find(i => i.id === issueId);
  const remaining = state.agents.filter(a => !i.members.includes(a.id));
  if (!remaining.length) return toast('所有成员都已在这个议题中。');
  openModal('邀请成员',`${field('加入议题的成员',`<select class="input" name="agent_id">${remaining.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('')}</select>`)}${formActions('邀请加入')}`,async form => {
    await mutate('invite',{issue_id:issueId,agent_id:new FormData(form).get('agent_id')}); closeModal(); toast('成员已加入议题。');
  });
}
function supplement(issueId) {
  openModal('补充要求',`${field('给议题成员的补充说明',textarea('body','你希望补充的约束、问题或判断依据','','required maxlength="12000"'))}${formActions('发送到群聊')}`,async form => {
    await mutate('message',{issue_id:issueId,body:new FormData(form).get('body')}); closeModal(); openIssue(issueId); toast('补充要求已保存到群聊。');
  });
}
async function connectionRequest(path, body) {
  return request(`/api/connection/${path}`,{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify(body)});
}
let connectionPlan;
async function connect(agentId, remove = false) {
  const a = member(agentId);
  openModal(remove ? '撤销应用接入' : `连接 ${a.name}`, '<p class="modal-lead">正在自动检测应用配置…</p>');
  const loadingId=String(performance.now());modal.dataset.connectionLoading=loadingId;
  try {
    connectionPlan = await connectionRequest('prepare',{agent_id:agentId,remove});
    if (!modal.open || modal.dataset.connectionLoading!==loadingId) return;
    const config = await request(`/api/config?agent=${encodeURIComponent(agentId)}`);
    openModal(remove ? '撤销应用接入' : `连接 ${a.name}`,`<div class="connect-identity">${appIcon(a.id)}<div><strong>${esc(a.name)}</strong><p>${connectionPlan.detected ? '已检测到本机应用配置' : '将为应用建立接入配置'}</p></div></div><div class="connect-stages"><span class="done">① 检测应用</span><span class="line"></span><span>② 你的授权</span><span class="line"></span><span>③ 应用确认</span></div><p class="modal-lead">${remove ? '移除此成员的协作入口。其他插件和工具配置会保留，并备份当前配置。' : '允许此成员通过本地协作工具参与任务。面板会自动完成配置，你无需复制代码或填写参数。'}</p>${remove ? '' : `<div class="permissions">${connectionPlan.permissions.map((line,j) => `<div class="permission">${icon(['tasks','chat','link'][j])}<span>${esc(line)}</span></div>`).join('')}</div>`}${!remove && !current().demo && current().workspace && ['codex','dsh'].includes(a.app) ? '<label class="checkbox-label launch-consent"><input type="checkbox" id="native-project-after-connect" checked>为当前项目自动创建并绑定原生工作会话</label>' : ''}${!remove && a.app!=='codex' && connectionPlan.launch_available ? '<label class="checkbox-label launch-consent"><input type="checkbox" id="launch-after-connect" checked>配置后打开对应的本地应用</label>' : ''}<button type="button" class="btn primary full" data-action="authorize-connect">${icon(remove ? 'close' : 'link')}${remove ? '确认撤销接入' : a.setup?.configured ? '授权更新连接' : '授权并连接'}</button><p class="connect-foot">${esc(connectionPlan.notice)}</p>${a.setup?.configured && !remove ? btn('disconnect','撤销此成员接入','','ghost full',`data-agent="${a.id}"`) : ''}<details class="advanced"><summary>高级详情</summary><p class="config-path">配置位置：${esc(connectionPlan.path)}</p><pre class="config" id="bridge-config">${esc(JSON.stringify(config,null,2))}</pre><div class="row">${btn('copy-config','复制配置参考','copy')}${btn('copy-kickoff','复制会话说明','file','',`data-agent="${a.id}"`)}</div></details>`);
    modal.dataset.connectionAgent=agentId;
  } catch(error) {
    if (modal.open && modal.dataset.connectionLoading===loadingId) openModal('暂时无法自动接入',`<p class="modal-lead">${esc(error.message)}</p><div class="form-actions">${btn('close-modal','知道了')}</div>`);
  }
}
async function authorizeConnect(el) {
  if (!connectionPlan) return;
  const launch=!!$('#launch-after-connect')?.checked;
  const project=$('#native-project-after-connect')?.checked ? current().id : null;
  el.disabled=true;el.textContent='正在配置，请稍候…';
  try {
    const result=await connectionRequest('commit',{plan_id:connectionPlan.plan_id,consent:true,project_id:project});
    let opened=false;
    if(result.configured && launch) {try {const openedResult=await request('/api/application/open',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({agent_id:result.agent_id})});opened=openedResult.opened;} catch(error) {toast(error.message,true);}}
    state=await request('/api/state');render();
    openModal(result.configured ? '接入状态' : '已撤销接入',`<div class="connect-result">${icon('check')}<h3 id="connection-status">${result.configured ? '配置已写入，等待原应用加载工具' : '接入配置已撤销'}</h3><p>${result.configured ? (opened ? '已请求打开本地应用。' : '')+'请在原应用重新加载协作工具；面板会分别显示工具调用、原生 Agent 在线和项目会话绑定状态。' : '此成员已停止通过面板接收和提交工作。应用重新加载后移除工具。'}</p>${btn('close-modal','完成','','primary')}</div>`);
    if(result.configured) {
      modal.dataset.connectionAgent=result.agent_id;
      if(result.native_session) {
        modal.dataset.connectionProject=project;
        $('#connection-status').textContent='连接已登记，正在自动创建或绑定项目会话';
      }
    }
  } catch(error) {
    toast(error.message,true);el.disabled=false;el.textContent='重新授权';
  }
}

function agentForm(agentId) {
  const a = agentId ? member(agentId) : null;
  openModal(a ? '编辑成员' : '添加成员',`${field('成员名称',input('name','例如：Codex · 代码审核',a?.name,'required maxlength="60"'))}${field('所属应用',`<select class="input" name="app" ${a ? 'disabled' : ''}>${Object.entries(apps).map(([id,app]) => `<option value="${id}" ${a?.app === id ? 'selected' : ''}>${esc(app.name)}</option>`).join('')}</select>`,'使用所属应用的图标。同一应用可以登记多个成员。')}${formActions()}`,async form => {
    const f = new FormData(form);
    await mutate('agent_save',{id:a?.id,name:f.get('name'),app:a?.app || f.get('app')}); closeModal(); toast('成员已保存。');
  });
}
function pause() {
  const p = current();
  if (p.paused) return mutate('pause',{paused:false}).then(() => toast('已恢复派发，并通知任务成员。'));
  openModal('暂停当前任务',`<p class="modal-lead">暂停派发新工作，并向此任务的成员发送暂停通知。已在其他应用中运行的操作，需要对应会话读取通知后配合停止；此按钮不会强制关闭应用。</p>${formActions('暂停并通知成员')}`,async () => {
    await mutate('pause',{paused:true}); closeModal(); toast('已暂停派发，成员信箱中已加入暂停通知。');
  });
}
function saveChat() {
  localStorage.setItem('relay.chat', ui.chat ? '1' : '0');
  localStorage.setItem('relay.chatMode', ui.chatMode);
  localStorage.setItem('relay.issue', ui.issue || '');
}
function chooseProject(id) {
  ui.project = id; ui.issue = null; ui.chat = false; ui.page = 'overview';
  localStorage.setItem('relay.project',id); saveChat(); render();
  if ($('.canvas')) $('.canvas').scrollTop = 0;
}
function openIssue(id) {
  ui.issue = id; ui.chatMode = 'issue'; ui.chat = true; saveChat(); render();
  if ($('#chat-scroll')) $('#chat-scroll').scrollTop = $('#chat-scroll').scrollHeight;
  // Only what is actually on screen is cleared; a mention above the fold stays
  // unread until the user scrolls to it.
  settleMentions();
}
// Clear the mentions the user can actually see in the open thread.
//
// Opening a thread is not reading it: the count must survive a refresh for
// everything that was never scrolled into view, and each issue keeps its own.
let settling = false;
async function settleMentions() {
  const p = current(); const t = currentThread(p);
  if (!p || !t || !ui.chat || settling) return;
  // A background tab, or a thread that is not the open one, must not clear a
  // notice: the user did not see it. Both are checked before any DOM geometry.
  if (document.visibilityState === 'hidden' || !document.hasFocus()) return;
  if (currentThreadId() !== t.id) return;
  const key = t.mode === 'project' ? 'project' : t.id;
  if (!mentionCount(p, key)) return;
  const scroller = $('#chat-scroll');
  if (!scroller) return;
  const box = scroller.getBoundingClientRect();
  if (!box.height) return;
  // Each message is checked on its own, so a partly scrolled list clears only the
  // mentions that are really inside the viewport.
  const visible = [...scroller.querySelectorAll('[data-mentions-user]')]
    .filter(node => {
      const rect = node.getBoundingClientRect();
      return rect.height > 0 && rect.bottom > box.top && rect.top < box.bottom;
    })
    .map(node => node.dataset.message);
  if (!visible.length) return;
  settling = true;
  try {
    await request('/api/action',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},
      body:JSON.stringify({action:'mentions_read',args:{project_id:p.id,issue_id:t.mode==='project'?null:t.id,message_ids:visible}})});
  } catch { /* a failed clear must not hide the dot; the next view retries. */ }
  finally { settling = false; }
}
// The open thread's identity, so a stale scroll event cannot clear another one.
function currentThreadId() {
  if (!ui.chat) return null;
  return ui.chatMode === 'project' ? 'project' : (ui.issue || null);
}
function openProjectChat() {
  ui.chatMode = 'project'; ui.chat = true; saveChat(); render();
  if ($('#chat-scroll')) $('#chat-scroll').scrollTop = $('#chat-scroll').scrollHeight;
  settleMentions();
}
// Jump from a red dot straight to the newest message addressed to the user.
function openMention(issueId) {
  openIssue(issueId);
  const scroller = $('#chat-scroll');
  if (!scroller) return;
  const nodes = [...scroller.querySelectorAll('[data-mentions-user]')];
  const target = nodes[nodes.length - 1];
  if (!target) return;
  target.scrollIntoView({block:'center'});
  target.classList.add('mention-flash');
  setTimeout(() => target.classList.remove('mention-flash'), 1600);
  settleMentions();
}
async function copy(value) {
  try {await navigator.clipboard.writeText(value);toast('已复制。');}
  catch {openModal('复制内容',`<p class="modal-lead">浏览器没有授予剪贴板权限，请选中下方内容手动复制。</p><textarea class="input" rows="12" readonly>${esc(value)}</textarea>`);}
}
async function sendMessage() {
  const t = currentThread();
  if (ui.submitting || !t) return;
  const body = (ui.drafts[t.id] || '').trim();
  const files = ui.files[t.id] || [];
  if (!body && !files.length) return toast('先写一条消息，或添加附件。');
  if (t.locked) return toast('任务已结束，群聊为只读记录。');
  ui.submitting = true;
  try {
    // Structured mentions: the server resolves and validates them, so naming one
    // member does not wake the whole team. Pure-text @ still works for callers
    // that cannot send ids.
    const mentioned = resolveMentions(body || '');
    const extra = mentioned.length ? {mentions:mentioned} : {};
    if (t.mode === 'project') await mutate('project_message',{body:body || '分享了附件',attachments:files,...extra});
    else await mutate('message',{issue_id:t.id,body:body || '分享了附件',attachments:files,...extra});
    ui.drafts[t.id] = ''; ui.files[t.id] = []; saveChat(); render();
  } finally {ui.submitting = false; render();}
}
// Resolve `@token` against the real member ids and display names, so what the
// server receives is an id it can verify rather than a guess from the text.
function resolveMentions(body){
  const found = [];
  // Code spans describe mentions rather than make them, so they are dropped
  // before scanning — same rule the server applies.
  const scannable = (body || '').replace(/```[\s\S]*?```/g, ' ').replace(/`[^`]*`/g, ' ');
  for (const match of scannable.matchAll(/@([^\s@,，。:：;；!！?？'"()\[\]<>|`]+)/g)) {
    const token = match[1].trim();
    const agent = (state.agents || []).find(a => a.id === token || a.name === token);
    if (agent && !found.includes(agent.id)) found.push(agent.id);
  }
  return found;
}
async function recordRecycle(kind,itemId,issueId){
  const label={task:'工作板块',issue:'议题',project_message:'群聊消息',issue_message:'议题消息'}[kind]||'记录';
  openModal(`回收${label}`,`<p class="modal-lead">${label}会移入记录回收区，随时可以恢复。原始内容不会被删除，导出与历史记录仍保留这条操作。</p>${kind==='issue'?'<p class="small muted">回收议题会解除它对关联板块的阻塞；若还有其它未解决议题，板块仍保持受议题影响。</p>':''}${kind==='task'?'<p class="small muted">只有待派发、已撤销或已验收的板块可以回收。</p>':''}${formActions('确认回收')}`,async () => {
    await mutate('record_archive',{kind,item_id:itemId,...(issueId?{issue_id:issueId}:{})});
    closeModal();toast(`${label}已移入回收区，可在「记录回收区」恢复。`);
  });
}
async function recordRestore(kind,itemId,issueId){
  await mutate('record_restore',{kind,item_id:itemId,...(issueId?{issue_id:issueId}:{})});
  toast('已恢复。');
}
function recordBin(){
  const p=current();
  const tasks=p.tasks.filter(t=>t.archived);
  const issues=p.issues.filter(i=>i.archived);
  const projectMsgs=(p.chat||[]).filter(m=>m.archived);
  const issueMsgs=p.issues.flatMap(i=>(i.messages||[]).filter(m=>m.archived).map(m=>({...m,issueId:i.id,issueTitle:i.title})));
  const row=(kind,item,label,extra='')=>`<div class="cleanup-item"><span class="grow"><strong>${esc(label)}</strong><br><span class="small muted">${esc(item.archived_at?datetime(item.archived_at):'')}${extra?` · ${esc(extra)}`:''}</span></span>${btn('record-restore','恢复','arrow','ghost',`data-kind="${kind}" data-item="${item.id}"${item.issueId?` data-issue="${item.issueId}"`:''}`)}</div>`;
  const section=(title,rows,empty)=>`<h3>${title}（${rows.length}）</h3><div class="cleanup-list">${rows.join('')||`<div class="no-decision">${empty}</div>`}</div>`;
  openModal('记录回收区',`<p class="modal-lead">这里只列出被回收的消息、议题和板块。恢复后会重新出现在原位置；议题恢复会重新阻塞它关联的板块。</p>`
    +section('群聊消息',projectMsgs.map(m=>row('project_message',m,m.body.slice(0,60))),'没有回收的群聊消息。')
    +section('议题消息',issueMsgs.map(m=>row('issue_message',m,m.body.slice(0,60),m.issueTitle)),'没有回收的议题消息。')
    +section('议题',issues.map(i=>row('issue',i,i.title,i.status==='open'?'曾未解决':'已解决')),'没有回收的议题。')
    +section('工作板块',tasks.map(t=>row('task',t,t.title,t.status)),'没有回收的工作板块。'));
}
function stageMeta(s) {
  const who = s.status === 'submitted' ? `${esc(name(s.actor || s.requested_by))} · ${esc(datetime(s.submitted))}` : `等待 ${esc(name(current().assignments.reporter))} 汇总 · ${esc(datetime(s.requested))}`;
  return `<div class="row small muted">${appIcon(s.actor || s.requested_by,true)}${who}${s.self_submitted ? ' · 监督自行提交' : ''}</div>`;
}
function stageBody(s) {
  return s.body ? `<pre>${esc(s.body)}</pre>` : '<p class="page-lead">这一阶段的汇总还没有正文。</p>';
}
// The rail lists recent titles only; a stage summary is a deliverable, so its
// text and the whole history must be openable without reading the project record.
function openStage(stageId) {
  const p = current();
  const s = (p.stages || []).find(s => s.id === stageId);
  if (!s) return toast('找不到这条阶段汇总，请刷新。', true);
  openModal(`阶段汇总 · ${s.title || '已验收板块'}`, `<div class="small muted">第 ${s.revision} 版 · 涉及 ${(s.task_ids || []).length} 个板块</div>${stageMeta(s)}${stageBody(s)}`);
}
function openStages() {
  const p = current();
  const stages = p.stages || [];
  const summarised = new Set(stages.filter(s => s.status === 'submitted').map(s => s.issue_id));
  const pending = p.issues.filter(i => !i.archived && i.status !== 'open' && !summarised.has(i.id));
  const rows = stages.length
    ? stages.slice().reverse().map(s => `<article class="delivery-panel"><div class="row between"><h2>${esc(s.title || '已验收板块')}</h2>${s.status === 'submitted' ? badge('已提交','green','check') : badge('等待汇总','blue','clock')}</div><div class="small muted">第 ${s.revision} 版 · 涉及 ${(s.task_ids || []).length} 个板块</div>${stageMeta(s)}${stageBody(s)}</article>`).join('')
    : '<p class="page-lead">还没有阶段汇总。监督判断某一议题或一组板块完成时，可以只汇总这一段。</p>';
  const gap = pending.length
    ? `<div class="no-decision">${icon('info')}已解决但还没有阶段汇总的议题：${pending.map(i => esc(i.title)).join('、')}</div>`
    : '';
  openModal(`阶段汇总记录 · ${p.title}`, `<div class="small muted">共 ${stages.length} 条；下面是全部历史，不只是最近几条。</div>${gap}${rows}`);
}
const actions = {
  'close-project':closeProject,
  'settings-cleanup-preview':settingsCleanupPreview,
  'settings-refresh':refreshStorage,
  'restore-project':restoreProject,
  'settings-cleanup-execute':settingsCleanupExecute,
  'forget-project':forgetProject,
  'cleanup-preview':cleanupPreview,
  'cleanup-execute':cleanupExecute,
  'cleanup-restore':el=>cleanupRestore(el.dataset.plan),
  'record-archive':el=>recordRecycle(el.dataset.kind,el.dataset.item,el.dataset.issue),
  'record-restore':el=>recordRestore(el.dataset.kind,el.dataset.item,el.dataset.issue),
  'record-bin':recordBin,
  'reset-demo':async () => {await mutate('demo_reset');ui.chatMode='issue';ui.issue='demo_issue';ui.chat=true;saveChat();render();toast('演示场景已恢复。');},
  'independent-native':el=>independentNative(el.dataset.agent),
'native-release':el=>releaseNativeCodex(el.dataset.agent),
  'create-native':async el=>{if(state.runtime?.bindings?.some(b=>b.project===current().id && b.agent===el.dataset.agent)) return independentNative(el.dataset.agent);await request('/api/runtime/create',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({project_id:current().id,agent_id:el.dataset.agent})});closeModal();toast('已请求创建原生工作会话，等待应用回执。');},
  'bind-native':el=>bindNative(el.dataset.agent),
  'native-connect':el=>connectNativeCodex(el.dataset.agent),
  'native-approval':el=>nativeApproval(el.dataset.agent,el.dataset.request),
  'native-decline':el=>nativeRespond(el.dataset.agent,el.dataset.request,{decision:'decline'}),
  'native-model':el=>chooseNativeModel(el.dataset.agent),
  'native-activity':el=>nativeActivity(el.dataset.agent),
  'choose-workspace':chooseWorkspace,'set-workspace':setWorkspace,
  'new-project':newProject,'assign':assign,'new-task':() => newTask(), 'new-issue':newIssue,
  'new-agent':() => agentForm(), 'edit-agent':el => agentForm(el.dataset.agent),
  'task-detail':el => taskDetail(el.dataset.task), 'edit-task':el => newTask(current().tasks.find(t => t.id === el.dataset.task)),
  'accept-task':el => acceptance(el.dataset.task,true), 'reject-task':el => acceptance(el.dataset.task,false),
  'invite':el => invite(el.dataset.issue), 'supplement':el => supplement(el.dataset.issue),
  'connect':el => connect(el.dataset.agent), 'disconnect':el => connect(el.dataset.agent,true), 'authorize-connect':authorizeConnect, 'pause':pause,
  'open-issue':el => openIssue(el.dataset.issue), 'open-issue-mention':el => openMention(el.dataset.issue), 'open-project-chat':openProjectChat, 'close-chat':() => {ui.chat=false;saveChat();render();},
  'close-modal':closeModal,
  'nav':el => {ui.page=el.dataset.page;render();if ($('.canvas')) $('.canvas').scrollTop=0;if (ui.page === 'settings' && !ui.storage) refreshStorage().catch(error => toast(error.message,true));},
  'deliveries':() => {ui.page='deliveries';render();},
  'delivery-project':el => {ui.project=el.dataset.project;ui.issue=null;ui.chat=false;ui.page='deliveries';localStorage.setItem('relay.project',ui.project);render();},
  'project':el => chooseProject(el.dataset.project),
  'dispatch':async () => {await mutate('dispatch');toast('任务已进入成员信箱，等待应用领取。');},
  'resolve':async el => {
    const i = current().issues.find(i => i.id === el.dataset.issue);
    await mutate('resolve',{issue_id:i.id,choice:ui.choices[i.id] || i.options[0]?.id});
    toast('你的决定已保存，并通知相关成员。');
  },
  'resolve-discussion':async el => {await mutate('resolve',{issue_id:el.dataset.issue});toast('议题已解决。');},
  'summary-request':async () => {await mutate('summary_request');toast('汇总任务已加入负责人信箱。');},
  'stage-view':el => openStage(el.dataset.stage),
  'stage-list':openStages,
  'export':() => {const a=document.createElement('a');a.href=`/api/export?project=${encodeURIComponent(current().id)}`;a.download='stage-relay-report.md';a.click();},
  'popout':() => {const url=new URL(location.href);url.search=`?project=${current().id}&chat=popout&mode=${ui.chatMode}${ui.chatMode==='issue' && ui.issue ? `&issue=${ui.issue}` : ''}`;window.open(url.href,'_blank','noopener,noreferrer,width=620,height=850');},
  'back':() => {const url=new URL(location.href);url.search='';url.searchParams.set('project',current().id);location.href=url.href;},
  'copy-config':() => copy($('#bridge-config').textContent),
  'copy-kickoff':el => copy(`你是 Stage Relay 成员 ${el.dataset.agent}（${name(el.dataset.agent)}）。请使用已登记的协作桥工具 relay_inbox 读取职责与任务，保留 cursor 并在每个工作阶段读取新消息。只执行分配给你的板块，在原应用中使用自己的工具；paused=true 时停止推进。进展通过 relay_report 汇报，完成后提交 review 和实际检查依据；遇到困难或新提案通过 relay_raise_issue 发起讨论，不代替用户决定。当前任务 ID：${current().id}。`),
  'send':sendMessage,
  'attach':() => {const f=$('#attachment-input');f.accept='';f.click();},
  'attach-image':() => {const f=$('#attachment-input');f.accept='image/*';f.click();},
  'remove-file':el => {const t=currentThread();if(!t)return;ui.files[t.id].splice(Number(el.dataset.index),1);render();},
  'mention':() => {
    const t = currentThread();
    if (!t) return;
    const list = t.members.filter(id => id !== 'user');
    openModal('提及成员',`<p class="modal-lead">${t.mode === 'project' ? '选择项目成员' : '选择成员'}，将名字插入消息；${t.mode === 'project' ? '项目群聊对所有任务成员可见' : '议题中的所有成员都可以读取群聊'}。</p><div class="stack">${list.length ? list.map(id => btn('insert-mention',esc(name(id)),'plus','',`data-agent="${id}"`)).join('') : '<div class="empty">当前项目还没有其他成员。</div>'}</div>`);
  },
  'insert-mention':el => {const t=currentThread();if(t)ui.drafts[t.id]=(ui.drafts[t.id] || '')+`@${name(el.dataset.agent)} `;closeModal();render();$('#chat-input')?.focus();},
};

// A rebuild between mousedown and mouseup removes the pressed element, so the
// click never fires and the button looks dead — the user presses again and it
// works "sometimes". Remember what was pressed on pointerdown and, if the click
// lands on something else, resolve the action from that record.
let pressedAction = null;
document.addEventListener('pointerdown',event => {
  const el = event.target.closest?.('[data-action]');
  pressedAction = el && !el.disabled
    ? {action: el.dataset.action, dataset: {...el.dataset}, at: Date.now()}
    : null;
}, true);
document.addEventListener('click',async event => {
  let el = event.target.closest('[data-action]');
  if (!el && pressedAction && Date.now() - pressedAction.at < 1500) {
    // Only a missing target indicates replacement between down and click.
    // A disabled replacement is a fresh decision and must stay disabled.
    el = {dataset: pressedAction.dataset, disabled: false};
  }
  pressedAction = null;
  if (!el || el.disabled) return;
  const action = actions[el.dataset.action];
  if (action) {
    try {await action(el);} catch (error) {toast(error.message,true);}
  }
});
document.addEventListener('submit',async event => {
  if (event.target.id !== 'modal-form') return;
  event.preventDefault();
  const submit = $('button[type="submit"]',event.target);
  submit.disabled = true;
  try {await modalSubmit?.(event.target);} catch (error) {$('#form-error').hidden=false;$('#form-error').textContent=error.message;}
  finally {if (submit.isConnected) submit.disabled=false;}
});
document.addEventListener('input',event => {
  if (event.target.id === 'chat-input') {const t=currentThread();if(t)ui.drafts[t.id]=event.target.value;}
});
document.addEventListener('compositionstart',event => {
  if (event.target.id === 'chat-input') ui.composing=true;
});
document.addEventListener('compositionend',event => {
  if (event.target.id !== 'chat-input') return;
  ui.composing=false;
  const t=currentThread();if(t)ui.drafts[t.id]=event.target.value;
  // Keep the focused composer mounted even after the IME commit; a full
  // refresh occurs on blur or when the user explicitly opens another thread.
});
document.addEventListener('focusout',event => {
  if (event.target.id === 'chat-input' && !ui.composing && ui.pendingRender) {ui.pendingRender=false;queueMicrotask(() => {if (document.activeElement?.id !== 'chat-input') render();else ui.pendingRender=true;});}
});
document.addEventListener('toggle',event => {if (event.target.classList?.contains('exec-drawer')) ui.execOpen = event.target.open;}, true);
// Scrolling is how a message enters view without clicking anything: observe it so
// "actually looked at" is honoured, not only when a thread is first opened.
let settleTimer = null;
const queueSettle = () => {clearTimeout(settleTimer); settleTimer = setTimeout(() => {settleMentions();}, 180);};
document.addEventListener('scroll',queueSettle,true);
document.addEventListener('wheel',queueSettle,{passive:true,capture:true});
document.addEventListener('keyup',event => {if (['PageUp','PageDown','ArrowUp','ArrowDown','Home','End',' '].includes(event.key)) queueSettle();});
document.addEventListener('visibilitychange',() => {if (document.visibilityState === 'visible') queueSettle();});
window.addEventListener('focus',queueSettle);
document.addEventListener('change',event => {
  const el = event.target;
  if (el.dataset.choice) {ui.choices[el.dataset.choice]=el.value;render();}
  if (el.id === 'needs-decision') {
    $('#decision-fields').hidden=!el.checked;
    $('#decision-fields').querySelectorAll('input,textarea').forEach(input => input.required=el.checked);
  }
});
document.addEventListener('keydown',event => {
  if (event.key === 'Escape' && !event.isComposing && !modal.open && !ui.standalone) {ui.chat=false;saveChat();render();}
  if (!event.isComposing && event.key === 'Enter' && (event.ctrlKey || event.metaKey) && event.target.id === 'chat-input') {event.preventDefault();sendMessage().catch(error => toast(error.message,true));}
});
$('#attachment-input').addEventListener('change',async event => {
  const input = event.target;
  const thread = currentThread();
  if (!thread || thread.locked) return;
  const issue = thread.id;
  const existing = ui.files[issue] || [];
  try {
    if (existing.length+input.files.length>5) throw new Error('一条消息最多 5 个附件。');
    for (const file of input.files) {
      if (file.size>5*1024*1024) throw new Error(`${file.name} 超过 5 MB。`);
      const data = await new Promise((resolve,reject) => {const reader=new FileReader();reader.onload=() => resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const result = await request('/api/upload',{method:'POST',headers:{'Content-Type':'application/json','X-Relay-Client':'panel'},body:JSON.stringify({name:file.name,data,project_id:current().id})});
      existing.push(result);ui.files[issue]=existing;
    }
  } catch(error) {toast(error.message,true);}
  finally {input.value='';render();}
});
// Volatile timestamps change on every poll without changing anything the page
// shows: a member's last contact, a host's freshness stamp, a job's updated
// field. Treating them as "state changed" rebuilt the whole panel every 2.5 s,
// which drops a click that lands between mousedown and mouseup — the user sees a
// button that "sometimes does nothing". Compare everything else.
const VOLATILE_KEYS = new Set(['last_seen', 'seen', 'updated']);
function renderKey(value) {
  if (Array.isArray(value)) return '[' + value.map(renderKey).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).filter(k => !VOLATILE_KEYS.has(k)).sort()
      .map(k => JSON.stringify(k) + ':' + renderKey(value[k])).join(',') + '}';
  }
  return JSON.stringify(value === undefined ? null : value);
}
// Native activity is written by a running agent on every step, so it changes on
// almost every poll. It is only rendered inside the chat drawer's execution log,
// so while that drawer is closed it cannot change anything on screen — counting
// it rebuilt the whole panel every 2.5 s under a running session, which replaced
// buttons between pointerdown and click and made them look dead.
function comparableState(data) {
  if (!data) return data;
  if (ui.chat || !data.runtime || typeof data.runtime !== 'object') return data;
  const runtime = {...data.runtime};
  delete runtime.activity;
  return {...data, runtime};
}
async function poll() {
  if (ui.polling) return;
  ui.polling = true;
  try {
    const next = await request('/api/state');
    const changed = !state || renderKey(comparableState(state)) !== renderKey(comparableState(next)) || ui.offline;
    state = next;ui.offline = false;
    if (changed) {
      if (ui.composing || document.activeElement?.id === 'chat-input') ui.pendingRender = true;
      else { ui.pendingRender = false; try {render();} catch(error) {toast(`页面渲染出错：${error.message}`,true);} }
    }
    if (modal.open && modal.dataset.connectionAgent && $('#connection-status')) {const agent=member(modal.dataset.connectionAgent);const bound=state.runtime?.bindings.some(b=>b.agent===modal.dataset.connectionAgent && b.project===modal.dataset.connectionProject);const host=state.runtime?.hosts?.find(h=>h.agent===modal.dataset.connectionAgent && h.online);const codexHost=state.codexNative?.find(h=>h.agent===modal.dataset.connectionAgent && h.connected);if(bound) $('#connection-status').textContent='已连接，并绑定项目原生工作会话';else if(codexHost) $('#connection-status').textContent='ChatGPT 内置 Codex 原生 Agent 已连接，等待项目工作会话';else if(host) $('#connection-status').textContent=`${host.app==='dsh'?'DSH':'原应用'}原生插件已在线，等待项目工作会话`;else if(agent?.last_seen) $('#connection-status').textContent='协作工具已收到调用，原生 Agent 尚未确认在线';}
  } catch {if (!ui.offline) {ui.offline=true;if (ui.composing || document.activeElement?.id === 'chat-input') ui.pendingRender=true;else {try {render();} catch(error) {toast(error.message,true);}}}}
  finally {ui.polling=false;}
}
function bootMessage(error, override) {
  // A render fault must never be reported as "the panel is not running": that
  // sends the user to restart a service that is actually up. Show what broke.
  const offline = override ? override.offline : ui.offline;
  const loaded = override ? override.hasState : !!state;
  return !offline && loaded
    ? `<div class="boot">页面渲染出错<span>${esc(error.message)}</span><span>本地服务在线，数据已保存在 workspace.sqlite3；刷新可重试，若持续出现请把这段提示发给项目成员。</span></div>`
    : `<div class="boot">无法连接本地面板<span>${esc(error.message)}</span><span>运行“启动 Stage Relay.cmd”，然后刷新此页面。</span></div>`;
}
try {
  state = await request('/api/state');
  const p = current();ui.project=p.id;
  localStorage.setItem('relay.project', ui.project);
  if (ui.issue && !currentIssue()) ui.issue = null;
  const remembered = localStorage.getItem('relay.chat');
  if (!remembered && p.demo && !ui.issue) {ui.issue=p.issues[0]?.id;ui.chat=true;}
  if (ui.chat && ui.chatMode === 'issue' && !ui.issue) ui.chatMode = 'project';
  saveChat();
  render();setInterval(poll,2500);
  if (ui.page === 'settings') refreshStorage().catch(error => toast(error.message,true));
} catch(error) {
  $('#app').innerHTML = bootMessage(error);
}
