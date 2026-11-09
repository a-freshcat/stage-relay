// Headless UI probe: renders the real web/app.js against a minimal DOM shim so
// the closed-project rail and the cleanup preview modal can be asserted without a
// browser. No panel process, no network: fetch answers from the qa fixture.
import {readFileSync, writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const root = resolve('.');
const fixture = JSON.parse(readFileSync(`${root}/qa/ui-fixture/state.json`, 'utf8'));
const closed = fixture.projects.find(p => p.title === '结束态渲染');
const open = fixture.projects.find(p => p.title === '进行中任务');

const stateCopy = JSON.parse(JSON.stringify(fixture));
const preview = {
  project: closed.id, title: closed.title, retention_days: 3,
  generated: '2026-11-08T00:00:00+00:00', closed: true,
  rules: ['只清理两类对象：临时目录与未引用的上传附件。'],
  items: [{path: 'C:/tmp/uploads/deadbeef', rel: 'uploads/deadbeef', size: 2048, kind: '孤儿附件', reason: '没有任何项目消息引用，且超过保留期 3 天'}],
  retained: [{path: 'C:/tmp/workspace.sqlite3', size: 81920, reason: '本应用状态与凭据，永不移动'}],
  totals: {move_items: 1, move_bytes: 2048, retained_items: 1, retained_bytes: 81920},
};

const prelude = `
const store = new Map();
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {
    id, innerHTML: '', textContent: '', scrollTop: 0, scrollHeight: 100, clientHeight: 50,
    dataset: {}, open: false, classList: {toggle(){}},
    addEventListener(){}, focus(){}, remove(){}, setSelectionRange(){}, append(){}, closest(){return null;},
    showModal(){this.open = true;}, close(){this.open = false;}, querySelectorAll(){return [];}, querySelector(){return null;},
  });
  return nodes.get(id);
}
const listeners = new Map();
const document = {
  querySelector: selector => node(selector), addEventListener(type,handler){listeners.set(type,handler);}, title: '', activeElement: null,
  body: {classList: {toggle(){}}}, createElement: () => node('created-' + Math.random()),
};
const localStorage = {getItem:k => store.has(k) ? store.get(k) : null, setItem:(k,v) => store.set(k, String(v))};
const location = {search: '', href: 'http://127.0.0.1:4399/'};
const window = {open(){}, pywebview: undefined};
const setInterval = () => 0;
const __responses = ${JSON.stringify({
  state: stateCopy,
  preview,
  closed: {result: closed, plan: {cancelled: 1, archived: 1, unbound: 0, retained: 0, retry: 0}, state: fixture},
  executed: {project: closed.id, root: 'C:/tmp/recovery/x', moved: [{path: 'a'}], skipped: [], failed: []},
})};
const fetch = async (path) => {
  const key = path.startsWith('/api/cleanup/preview') ? 'preview'
    : path === '/api/cleanup/execute' ? 'executed'
    : path === '/api/project/close' ? 'closed' : 'state';
  return {ok: true, json: async () => __responses[key]};
};
const navigator = {clipboard: {writeText: async () => {}}};
const FileReader = class { readAsDataURL() { this.onerror?.(new Error('unused')); } };
`;

const epilogue = `
globalThis.__probe = {ui, state: () => state, overview, date, datetime, closedRail, execLog, cards, settingsPage, bootMessage, forgetProject, cleanupPreview, closeProject, chat, currentThread, node, document, listeners, poll, nativeInfo, roleStatus, presence, effortField, resolveMentions, decisions, openStage, openStages, render, renderKey, actions, comparableState, issueCard, issuesPage, mentionCount, settleMentions, openMention};
export {};
`;

const source = readFileSync(`${root}/web/app.js`, 'utf8');
const probePath = `${root}/qa/.ui-probe.mjs`;
writeFileSync(probePath, prelude + '\n' + source + '\n' + epilogue);
await import(pathToFileURL(probePath).href + '?v=' + Date.now());
const probe = globalThis.__probe;

const failures = [];
const check = (label, condition, detail = '') => {
  console.log(`${condition ? 'PASS' : 'FAIL'} ${label}${condition ? '' : ' :: ' + detail}`);
  if (!condition) failures.push(label);
};

probe.ui.project = closed.id;
const rail = probe.closedRail(closed);
check('结束态侧栏显示结束信息', rail.includes('任务已结束') && rail.includes('保留期 3 天'), rail.slice(0, 200));
check('结束态侧栏保留清理入口', rail.includes('清理预览') && rail.includes('导出当前记录'));
check('结束态侧栏列出登记资源', rail.includes('dsh-native-demo') && rail.includes('已归档'), rail.slice(-300));
const view = probe.overview(closed);
check('结束态隐藏派发与新增入口', !view.includes("data-action=\"dispatch\"") && !view.includes("data-action=\"new-issue\"") && view.includes('任务已结束，不再派发'), view.length);
check('结束态仍保留项目群聊入口', view.includes("data-action=\"open-project-chat\""));

await probe.cleanupPreview();
const modal = probe.node('#modal').innerHTML;
check('清理预览弹窗标题与规则', modal.includes('清理预览') && modal.includes('保留期 3 天'), modal.slice(0, 160));
check('清理预览列出候选与大小', modal.includes('孤儿附件') && modal.includes('2.0 KB') && modal.includes('候选（1）'), modal.slice(0, 600));
check('清理预览列出保留项与合计', modal.includes('保留项（1）') && modal.includes('80.0 KB') && modal.includes('合计'));
check('清理预览提供可恢复移动按钮', modal.includes('data-action="cleanup-execute"') && modal.includes('移入可恢复区'));
check('清理预览没有渲染缺字段', !modal.includes('undefined') && !modal.includes('NaN'), modal.slice(0, 400));

probe.ui.project = open.id;
probe.closeProject();
const closeModal = probe.node('#modal').innerHTML;
check('结束确认弹窗包含二次确认', closeModal.includes('结束任务') && closeModal.includes('name="confirm"') && closeModal.includes('name="retention_days"'), closeModal.slice(0, 300));
check('结束确认弹窗说明会话处理范围', closeModal.includes('只归档面板为此项目创建的原生会话') && closeModal.includes('不关闭进程'));

probe.ui.project = closed.id;
probe.ui.chatMode = 'project';
probe.ui.pendingRender = false;
probe.node('#modal').open=false;
// Force the next poll to observe a changed revision.
probe.state().revision = -1;
probe.state().runtime = {probe:'before'};
probe.ui.polling = false;
probe.ui.offline = true;
probe.document.activeElement = {id:'chat-input'};
probe.listeners.get('compositionstart')({target:probe.document.activeElement});
await probe.poll();
check('输入法组合阶段保留输入框并延迟刷新', probe.ui.composing === true && probe.ui.pendingRender === true);
probe.document.activeElement.value = '中文已上屏';
probe.listeners.get('compositionend')({target:probe.document.activeElement});
check('输入法结束后保留草稿直至失焦', probe.ui.composing === false && probe.ui.pendingRender === true && probe.ui.drafts.project === '中文已上屏');
probe.document.activeElement = null;
probe.listeners.get('focusout')({target:{id:'chat-input'}});
await Promise.resolve();
check('输入框失焦后补刷新', probe.ui.pendingRender === false);

const reviewerProject = JSON.parse(JSON.stringify(open));
reviewerProject.paused=false;
reviewerProject.assignments.planner='codex';
reviewerProject.tasks=[{status:'review'}];
probe.state().runtime={hosts:[],bindings:[]};
probe.state().codexNative=[{agent:'codex',connected:false,connectionStatus:'failed',connectionError:'thread example already has an active writer',approvals:[]}];
check('审核断线显示等待且保留任务',probe.roleStatus(reviewerProject,'planner').includes('审核等待连接'));
const info=probe.nativeInfo(reviewerProject,{id:'codex',app:'codex'});
check('占用错误显示中文解释和重试',info.includes('已被另一个写入者持有') && info.includes('连接 / 重试 Codex'));
check('原始占用信息放入技术详情',info.includes('<details>') && info.includes('技术详情'));

probe.state().runtime={hosts:[{agent:'codex',online:true,sessions:[]}],bindings:[]};
check('原生在线时不再要求等待工具确认',probe.presence({id:'codex',last_seen:null,setup:{configured:true}}).includes('原生连接在线'));

// Execution drawer must show the bound session, its generation and keep the
// source of every row so panel chat and native output are never conflated.
const rtState = probe.state();
const closedProject = rtState.projects.find(p => p.id === closed.id);
closedProject.chat = [{id:'m1',actor:'user',body:'面板消息',created:'2026-11-08T10:00:00+00:00',attachments:[]}];
rtState.runtime = {bindings:[{project:closed.id,agent:'deepseek',session:'session-abcdef123456',generation:2}],
  activity:[{agent:'deepseek',session:'session-abcdef123456',seq:7,type:'tool/call',data:{name:'pwsh'},created:'2026-11-08T10:01:00+00:00'}],
  jobs:[{id:'j1',project:closed.id,agent:'deepseek',session:'session-abcdef123456',status:'delivered',updated:1794140000,
    detail:{kind:'project_message',receipt:'enqueued',generation:2,merged_events:[11,12]}}],
  hosts:[],resources:[]};
const drawer = probe.execLog(closedProject);
check('执行抽屉标注绑定会话与轮次', drawer.includes('abcdef123456') && drawer.includes('绑定轮次 2'), drawer.slice(0,200));
check('执行抽屉显示原生工具与回执来源', drawer.includes('tool/call') && drawer.includes('project_message') && drawer.includes('代次 2'));
check('执行抽屉列出合并事件 id', drawer.includes('11') && drawer.includes('12'));
check('执行抽屉说明可能不是当前窗口', drawer.includes('可能不是你正在看的窗口'));
const chatHtml = probe.chat(closedProject);
check('群聊与执行输出同页且来源分开', chatHtml.includes('exec-drawer') && chatHtml.includes('面板消息'), chatHtml.length);
check('未绑定会话时给出空态说明', probe.execLog({id:'x',chat:[]}).includes('还没有绑定执行会话'));


// Member card must state the actual bound session id and its live status.
rtState.runtime = {bindings:[{project:closed.id,agent:'deepseek',session:'session-abcdef123456',generation:3}],
  activity:[], jobs:[], resources:[],
  hosts:[{agent:'deepseek',online:true,app:'dsh',
    sessions:[{id:'session-abcdef123456',status:'running',model:'fixture',workspace:'C:/x'}]}]};
const cardHtml = probe.cards(closedProject);
check('成员卡展示绑定会话 ID', cardHtml.includes('session-abcdef123456'), cardHtml.slice(0,200));
check('成员卡展示绑定轮次', cardHtml.includes('轮次 3'));
check('成员卡如实显示执行中状态', cardHtml.includes('执行中'));
rtState.runtime.hosts[0].sessions[0].status = 'idle';
check('空闲时成员卡显示空闲', probe.cards(closedProject).includes('空闲'));
rtState.runtime.bindings = [];
check('未绑定时成员卡说明未绑定', probe.cards(closedProject).includes('尚未绑定执行会话'));


// Settings page: app-level cleanup entry plus record deletion, with the demo
// project and open projects refusing deletion.
const settingsHtml = probe.settingsPage(closedProject);
check('设置页有占用清理入口', settingsHtml.includes('应用占用预览') || settingsHtml.includes('清理预览'), settingsHtml.slice(0, 200));
check('设置页有删除项目入口', settingsHtml.includes('移入回收区') || settingsHtml.includes('移回列表'));
check('设置页声明工作区不动', settingsHtml.includes('工作区') && (settingsHtml.includes('不动') || settingsHtml.includes('不计入')));
const openProject = probe.state().projects.find(x => !x.closed && !x.demo) || closedProject;
check('未结束项目提示先结束再回收', probe.settingsPage(openProject).includes('先结束任务') || openProject.closed);
const demoProject = probe.state().projects.find(x => x.demo);
check('演示项目不允许删除', demoProject ? probe.settingsPage(demoProject).includes('演示任务不能删除') : true);


// A delivery row without a timestamp used to throw RangeError("Invalid time
// value") during the first render, leaving the boot error page. Both helpers
// must degrade to a readable placeholder instead of throwing.
check('缺时间戳的任务行不再抛错', (() => {
  try {
    rtState.runtime.bindings = [{project:closed.id,agent:'deepseek',session:'s',generation:1}];
    rtState.runtime.jobs = [{id:'j2',project:closed.id,agent:'deepseek',session:'s',status:'queued',detail:{kind:'message'}}];
    const html = probe.execLog(closedProject);
    return html.includes('时间未知');
  } catch (error) { return false; }
})());
check('日期助手容忍空值与非法值', probe.date(undefined) === '时间未知' && probe.date('not-a-date') === '时间未知' && probe.datetime(null) === '时间未知');
check('日期助手接受秒级时间戳', probe.date(1794140000) !== '时间未知');


// A render fault must not be labelled "the panel is not running".
check('渲染出错不冒充服务未启动', (() => {
  const html = probe.bootMessage(new RangeError('Invalid time value'), {offline:false, hasState:true});
  return html.includes('页面渲染出错') && !html.includes('无法连接本地面板');
})());
check('真实连接失败才提示启动服务', probe.bootMessage(new Error('Failed to fetch'), {offline:true, hasState:false}).includes('无法连接本地面板'));


// The recycle area must be reachable and reversible from the settings page.
const recycledHtml = probe.settingsPage(closedProject);
check('设置页有移回列表入口', recycledHtml.includes('回收区'));
probe.ui.recycled = [{id:'project_gone', title:'已回收项目', tasks:1, issues:1, chat:2, size:2048}];
probe.ui.storage = [{project:'project_gone', title:'已回收项目', live:false, uploads_bytes:1024, uploads_count:2, scratch_bytes:0, scratch_files:0, recovery_bytes:512, recovery_items:1, registered:true}];
const withRecycle = probe.settingsPage(closedProject);
check('回收区列出可恢复记录', withRecycle.includes('已回收项目') && withRecycle.includes('移回列表'));
check('存储占用按项目显示容量', withRecycle.includes('设置与占用') && /KB|B<\//.test(withRecycle));
check('移入回收区说明会先结束项目', withRecycle.includes('先结束任务') || closedProject.closed);

// Thinking depth is a real per-route capability: it must render only for a route
// that reports efforts, default to the route default, and disappear otherwise
// rather than offering a value the app would refuse.
const withEfforts = [{provider:'p',model:'m',label:'P / M',efforts:['low','medium','high'],effortDefault:'medium'}];
const noEfforts = [{provider:'p',model:'plain',label:'P / Plain'}];
const effortHtml = probe.effortField(withEfforts, 0);
check('思考深度选择器按路由渲染', effortHtml.includes('思考深度') && effortHtml.includes('name="native_effort"'));
check('思考深度默认选中路由默认值', /<option value="medium" selected>/.test(effortHtml));
check('思考深度列出全部受支持值', ['low','medium','high'].every(e => effortHtml.includes(`value="${e}"`)));
check('无推理能力的路由不显示思考深度', probe.effortField(noEfforts, 0) === '');

// The writer holder is not observable from the panel: the app-server protocol
// carries no holder identity. The UI must say so and offer a release path
// instead of telling the user to hunt for a lock.
rtState.runtime.bindings = [{project:closed.id,agent:'codex',session:'01a1bbcb-7ac0-7393-9c67-a42022246540',generation:1}];
rtState.codexNative = [{agent:'codex', connected:false,
  connectionError:'thread 01a1bbcb-7ac0-7393-9c67-a42022246540 already has an active writer'}];
const writerHtml = probe.cards(closedProject);
check('active writer 报出确切线程', writerHtml.includes('01a1bbcb-7ac0-7393-9c67-a42022246540'));
check('持有者如实标未知', writerHtml.includes('未知'));
check('提供断开本面板连接', writerHtml.includes('断开本面板 Codex 连接'));
// Negative form: the panel must not *instruct* the user to delete a lock or
// close the shared desktop application. Saying it does neither is fine, so the
// pattern only matches an imperative aimed at the user.
check('不要求用户删锁或关共享应用', !/(请|你需要|建议)[^。]{0,12}(删锁|删除锁|关闭桌面端 Codex)/.test(writerHtml));
rtState.codexNative = [];
rtState.runtime.bindings = [];

// Mention routing: naming one member must resolve to that member's id, and must
// not silently resolve to everyone (the user's complaint about @Codex waking all).
check('@ 解析为真实成员 id', JSON.stringify(probe.resolveMentions('@codex 请看这条')) === '["codex"]');
check('@ 多个成员取并集', JSON.stringify(probe.resolveMentions('@codex @deepseek 一起看')) === '["codex","deepseek"]');
check('没有 @ 时不发送 mentions', probe.resolveMentions('大家都看一下').length === 0);
check('@ 无关名字不产生 mentions', probe.resolveMentions('@nobody 这是谁').length === 0);

// Stage summaries are deliverables: the rail may list only recent titles, but the
// text and the whole history must open from the panel, and a resolved issue with
// no stage must be named rather than silently absent.
const stageProject = JSON.parse(JSON.stringify(open));
// A copy that keeps the same id would be shadowed by the original in current().
stageProject.id = 'project_stage_ui';
stageProject.title = '阶段界面任务';
stageProject.stages = [
  {id:'stage_old', issue_id:'issue_a', title:'第一阶段', task_ids:['t1'], revision:1,
   requested_by:'codex', requested:'2026-11-08T09:00:00+00:00', status:'submitted',
   body:'第一阶段正文：工具目录已更新。', submitted:'2026-11-08T09:05:00+00:00', actor:'codex', self_submitted:true},
  {id:'stage_mid', issue_id:'issue_b', title:'第二阶段', task_ids:[], revision:1,
   requested_by:'codex', requested:'2026-11-08T10:00:00+00:00', status:'requested',
   body:'', submitted:null, actor:null},
  {id:'stage_new', issue_id:'issue_c', title:'第三阶段', task_ids:['t2'], revision:2,
   requested_by:'codex', requested:'2026-11-08T11:00:00+00:00', status:'submitted',
   body:'第三阶段正文：面板可查看全部历史。', submitted:'2026-11-08T11:05:00+00:00', actor:'codex'},
  {id:'stage_four', issue_id:'issue_d', title:'第四阶段', task_ids:[], revision:1,
   requested_by:'codex', requested:'2026-11-08T12:00:00+00:00', status:'submitted',
   body:'第四阶段正文。', submitted:'2026-11-08T12:05:00+00:00', actor:'codex'},
  {id:'stage_five', issue_id:'issue_e', title:'第五阶段', task_ids:[], revision:1,
   requested_by:'codex', requested:'2026-11-08T13:00:00+00:00', status:'submitted',
   body:'第五阶段正文，应出现在全部历史里。', submitted:'2026-11-08T13:05:00+00:00', actor:'codex'},
];
stageProject.issues = (stageProject.issues || []).concat([
  {id:'issue_gap', title:'尚未汇总的议题', status:'resolved', members:['codex','deepseek'], task_ids:[], created:'2026-11-08T08:00:00+00:00', options:[], messages:[]},
]);
probe.state().projects.push(stageProject);
probe.ui.project = stageProject.id;
const stageRail = probe.decisions(stageProject);
check('侧栏阶段汇总提供正文入口', stageRail.includes('data-action="stage-view"'), stageRail.length);
check('侧栏阶段汇总提供全部历史入口', stageRail.includes('data-action="stage-list"') && stageRail.includes('全部阶段记录 · 5'));
probe.openStage('stage_new');
const one = probe.node('#modal').innerHTML;
check('阶段正文可查看', one.includes('第三阶段正文：面板可查看全部历史。'), one.slice(0, 200));
check('阶段正文标注修订与板块数', one.includes('第 2 版') && one.includes('涉及 1 个板块'));
check('阶段正文标注提交人', /Codex|codex/.test(one), one.slice(0,200));
probe.openStage('stage_old');
const selfMade = probe.node('#modal').innerHTML;
check('自行提交的阶段如实标注', selfMade.includes('监督自行提交'), selfMade.slice(0,200));
probe.openStages();
const all = probe.node('#modal').innerHTML;
check('全部阶段记录列出全部历史', ['第一阶段','第二阶段','第三阶段','第四阶段','第五阶段'].every(t => all.includes(t)), all.length);
check('全部阶段记录显示各自正文', all.includes('第一阶段正文：工具目录已更新。') && all.includes('第五阶段正文，应出现在全部历史里。'));
check('全部阶段记录标出等待汇总的阶段', all.includes('等待汇总'));
check('全部阶段记录指出缺少汇总的已解决议题', all.includes('尚未汇总的议题'), all.slice(0, 300));
check('全部阶段记录无缺字段', !all.includes('undefined') && !all.includes('NaN'), all.slice(0, 300));
probe.openStage('does-not-exist');
check('未知阶段给出可读提示而不是空弹窗', probe.node('#modal').innerHTML.includes('阶段汇总') === true);

// A poll that only bumps volatile timestamps must not rebuild the DOM: a rebuild
// between mousedown and mouseup swallows the click, which looks like a dead
// button. Real changes must still render.
const sameShape = JSON.parse(JSON.stringify(probe.state()));
check('仅有易变时间戳时不重绘', probe.renderKey(probe.state()) === probe.renderKey(sameShape));
sameShape.agents = sameShape.agents.map(a => ({...a, last_seen: '2026-11-09T23:59:59+00:00'}));
check('成员最近联系时间不算内容变化', probe.renderKey(probe.state()) === probe.renderKey(sameShape));
// Start from a state that already carries runtime data, then move only the stamps.
probe.state().runtime = {hosts: [{agent:'codex', online:true, seen: 1}], jobs: [{id:'j', updated: 2, status:'queued'}], bindings: []};
const stamped = JSON.parse(JSON.stringify(probe.state()));
stamped.runtime.hosts[0].seen = 987654;
stamped.runtime.jobs[0].updated = 987654;
check('主机与任务时间戳不算内容变化', probe.renderKey(probe.state()) === probe.renderKey(stamped));
stamped.runtime.jobs[0].status = 'delivered';
check('任务状态变化触发重绘', probe.renderKey(probe.state()) !== probe.renderKey(stamped));
stamped.runtime.jobs[0].status = 'queued';
sameShape.revision = (probe.state().revision || 0) + 1;
check('新事件确实触发重绘', probe.renderKey(probe.state()) !== probe.renderKey(sameShape));
sameShape.revision = probe.state().revision;
sameShape.agents[0].session = 'changed-session';
check('成员会话变化触发重绘', probe.renderKey(probe.state()) !== probe.renderKey(sameShape));

// A rebuild between pointerdown and click removes the pressed button, so the
// browser fires the click on a detached or different node. The action recorded at
// pointerdown must still run, otherwise the control looks dead to the user.
await (async () => {
  const down = probe.listeners.get('pointerdown');
  const click = probe.listeners.get('click');
  probe.ui.page = 'overview';
  const pressed = {dataset: {action: 'nav', page: 'deliveries'}, disabled: false};
  down({target: {closest: () => pressed}});
  // The element is gone by the time the click arrives.
  await click({target: {closest: () => null}});
  check('按下后节点被重建，操作仍然执行', probe.ui.page === 'deliveries', probe.ui.page);
})();
await (async () => {
  const down = probe.listeners.get('pointerdown');
  const click = probe.listeners.get('click');
  probe.ui.page = 'overview';
  down({target: {closest: () => null}});
  await click({target: {closest: () => null}});
  check('没有按下的点击不会误触发', probe.ui.page === 'overview', probe.ui.page);
})();
await (async () => {
  const down = probe.listeners.get('pointerdown');
  const click = probe.listeners.get('click');
  probe.ui.page = 'overview';
  down({target: {closest: () => ({dataset: {action: 'nav', page: 'deliveries'}, disabled: false})}});
  await click({target: {closest: () => ({dataset: {action: 'nav', page: 'settings'}, disabled: true})}});
  check('被禁用的替换按钮保持禁用', probe.ui.page === 'overview', probe.ui.page);
})();

// A running session writes native activity constantly; that only shows inside
// the chat drawer's execution log. While the drawer is closed it must not cause a
// rebuild, or a click landing mid-rebuild does nothing.
await (async () => {
  probe.ui.chat = null;
  const a = JSON.parse(JSON.stringify(probe.state()));
  const b = JSON.parse(JSON.stringify(probe.state()));
  a.runtime = {activity: [{seq: 1, type: 'tool/call'}, {seq: 2, type: 'tool/result'}]};
  b.runtime = {activity: [{seq: 3, type: 'tool/call'}]};
  check('抽屉关闭时活动变化不重绘',
    probe.renderKey(probe.comparableState(a)) === probe.renderKey(probe.comparableState(b)));
  probe.ui.chat = 'issue:1';
  check('抽屉打开时活动变化触发重绘',
    probe.renderKey(probe.comparableState(a)) !== probe.renderKey(probe.comparableState(b)));
  probe.ui.chat = null;
})();

// Mentioning the user is a deliverable, not decoration: the entry needs a red dot
// with a count, per issue, surviving a refresh, and cleared only for messages that
// were actually on screen.
const mentionProject = JSON.parse(JSON.stringify(open));
mentionProject.id = 'project_mention_ui';
mentionProject.title = '提及提醒任务';
mentionProject.mention_counts = {'issue_m1': 2, 'issue_m2': 1, 'project': 1};
mentionProject.mention_total = 4;
const mentionBase = mentionProject.issues[0];
mentionProject.issues = ['issue_m1', 'issue_m2', 'issue_m3'].map((id, n) => ({
  ...mentionBase, id, title: ['第一个议题', '第二个议题', '第三个议题'][n],
  status: 'open', options: [], archived: false, messages: [],
  members: ['codex', 'deepseek'],
}));
rtState.projects.push(mentionProject);
probe.ui.project = mentionProject.id;

check('议题入口红点带未读计数',
  probe.issuesPage(mentionProject).includes('mention-dot') &&
  probe.issuesPage(mentionProject).includes('>2</b>'));
check('没有提及的议题不显示红点',
  !probe.issueCard(mentionProject.issues[2]).includes('mention-dot'));
// The sidebar badge is drawn by render(); the topbar project-chat entry shows the
// project-thread count in overview().
check('未读计数进入项目群聊入口', probe.overview(mentionProject).includes('@1 未读'),
  probe.overview(mentionProject).match(/项目群聊[^<]*/)?.[0] || 'no entry');

// A refresh must restore the count: it comes from the server snapshot, not from
// anything the panel remembers locally.
const beforeReload = probe.state().projects.find(x => x.id === mentionProject.id).mention_total;
check('刷新后未读计数仍在', beforeReload === 4, String(beforeReload));

// Messages addressed to the user are marked, and messages that only talk about the
// token in code are not.
mentionProject.chat = [
  {id:'msg_plain', actor:'codex', body:'普通进度', created:'2026-11-09T10:00:00+00:00'},
  {id:'msg_user', actor:'codex', body:'@user 请确认', created:'2026-11-09T10:01:00+00:00', mentions_user:true},
  {id:'msg_code', actor:'codex', body:'示例 `@user` 不触发', created:'2026-11-09T10:02:00+00:00'},
];
const mentionChatHtml = probe.chat(mentionProject);
check('提到你的消息有可见标记', mentionChatHtml.includes('提到你') && mentionChatHtml.includes('data-message="msg_user"'));
check('代码里的 @user 不被标记', !mentionChatHtml.includes('data-message="msg_code" data-mentions-user'));

// Visibility decides the clear: fetching while the chat is closed, or with nothing
// in view, must not consume the notice.
await (async () => {
  const calls = [];
  globalThis.__probeRequests = calls;
  probe.ui.chat = null;
  await probe.settleMentions();
  check('后台收到时不自动清除', calls.length === 0, JSON.stringify(calls));
})();

console.log(failures.length ? `\n${failures.length} FAILED` : '\nALL UI CHECKS PASSED');
process.exit(failures.length ? 1 : 0);
