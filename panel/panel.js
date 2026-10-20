"use strict";
const $ = id => document.getElementById(id);
const roleNames = {planner: "规划 agent", worker: "执行 agent", reviewer: "模型审核"};
const labels = {completed:"独立检查通过",running:"检查点：运行中",ready:"准备下一步",awaiting_recovery:"等待恢复",needs_manual_verification:"执行结束 · 未运行项目测试",blocked:"任务受阻",exhausted:"达到重试上限",accepted:"阶段结果已接纳",finished:"进程已结束",failed:"阶段失败",interrupted:"阶段未结束",unreadable:"日志暂不可读"};
labels.awaiting_plan="等待调用方调整方案";
Object.assign(labels,{stopped:"任务已停止",queued:"已排队",needs_replan:"需要调整方案"});
labels.user_confirmed="你已确认 · 流程已结束";
labels.review_paused="审核返修已暂停";
const phaseChoice = {}, outputScroll = {};
let current = "", snapshotKey = "", runsKey = "", busy = false, listTick = 0;
let pendingJob = "", launchSignature = "", launchKey = "";
let lastDeleted = "", lastDeletedIds = [], historyBusy = false, historyRows = [];
const selectedRuns = new Set();
let reviewChoice = null, reviewChoices = [], reviewBusy = false;
let callerDraft = null, launchEnabled = false;
let launchProfiles=[],launchRevision="",workerEpoch=0,repeatReady=false;
let currentActive=false,lastAutomaticRefresh=0;
let loadedDraftGoal="";
let loadedDraftId="",dismissedDraftId="",draftLoading=false,lastDraftCheck=0;
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
function pretty(value) { return typeof value === "string" ? value : JSON.stringify(value, null, 2); }
function badge(status) { return node("span", labels[status] || status || "—", "badge " + (["completed","accepted","user_confirmed"].includes(status)?"good":["running","ready"].includes(status)?"active":["failed","blocked","exhausted","awaiting_recovery"].includes(status)?"bad":"")); }
function detail(title, text, key) {
  const el = node("details"); el.dataset.key = key; el.append(node("summary",title),node("pre",pretty(text))); return el;
}
function number(value) { return typeof value === "number" ? value.toLocaleString() : "未知"; }
function readable(host, value, options = {}) {
  if(!value||typeof value!=="object")return;
  const line=(label,text)=>{if(text!==undefined&&text!==null&&text!==""){const p=node("p",undefined,"handoff-line");p.append(node("strong",label+"："),node("span",typeof text==="string"?text:pretty(text)));host.append(p);}};
  for(const [key,label] of Object.entries({request:"用户任务",goal:"目标"}))line(label,value[key]);
  line("执行模型的回答",workerAnswer(value));
  if(["approved","changes_requested","unable_to_verify"].includes(value.status)){
    line("审核结论",reviewLabel(value.status));line("审核说明",value.summary);
  }
  // Each field keeps its own meaning: a completed result has no overall "reason";
  // only replanning shows an adjustment reason and only blocked shows a blocker.
  if(value.status==="needs_replan"){
    line("调整原因",value.reason);line("受阻步骤",value.failedStep);line("需要调整的决策",value.decisionNeeded);
  }else if(value.status==="blocked"){
    line("阻塞原因",value.blockedReason);line("受阻步骤",value.failedStep);
  }
  if(value.status&&!options.skipStatus)line("结果",reportText(value.status));
  for(const [key,label] of Object.entries({steps:"实施步骤",decisions:"决策与依据",codeFacts:"代码事实",files:"相关文件与事实",constraints:"执行约束",changed:"实际改动",attempted:"已尝试",evidence:"依据",residual:"遗留事项",acceptance:"验收要求",expectedScope:"预期修改范围",mayAdjust:"允许自行调整",escalateWhen:"需要交回规划的情况"})){
    if(!Array.isArray(value[key])||!value[key].length)continue;
    const names={id:"编号",path:"文件",do:"操作",verify:"验证",what:"决策",why:"决策依据",note:"事实",symbol:"符号",lines:"行",summary:"说明",command:"命令",result:"结果",passed:"通过"};
    const list=node(key==="steps"?"ol":"ul");
    for(const item of value[key]){const li=node("li");if(typeof item==="string")li.textContent=item;else if(item&&typeof item==="object"){
      for(const [k,v] of Object.entries(item).filter(([,v])=>v!==""&&v!==null&&v!==undefined))li.append(node("div",(names[k]||k)+"："+(typeof v==="string"?v:pretty(v))));
    }list.append(li);}
    if(key==="steps"&&options.stepsInDetails){
      const box=node("details",undefined,"nested-details");
      box.append(node("summary","实施步骤（"+value[key].length+"）"),list);host.append(box);
    }else{host.append(node("strong",label),list);}
  }
  for(const [key,label] of Object.entries({plan:"收到的计划",previous_result:"执行端反馈",checks:"本地检查反馈"})){
    if(!value[key])continue;host.append(node("h3",key==="checks"&&value.status?"执行端自报检查":label));
    if(Array.isArray(value[key]))for(const check of value[key]){line(check.id||"检查",check.summary||check.failure||("退出码 "+check.exit_code));for(const k of ["stdout_tail","stderr_tail","stdout_path","stderr_path"])line(k,check[k]);}
    else readable(host,value[key],options);
  }
}
function reportText(status){return status==="completed"?"执行端报告完成":status==="needs_replan"?"执行端要求调整方案":status==="blocked"?"执行端报告阻塞":labels[status]||status;}
function textOf(value){return typeof value==="string"?value:value===undefined||value===null?"":pretty(value);}
function itemText(value){if(value===undefined||value===null)return "";if(typeof value==="string")return value;
  if(typeof value==="object")return Object.entries(value).filter(([,v])=>v!==""&&v!==null&&v!==undefined).map(([k,v])=>k+"："+textOf(v)).join("；");return String(value);}
function strings(value){return Array.isArray(value)?value.map(itemText).filter(Boolean):[];}
function checkPassed(check){return check&&check.exit_code===0&&!check.failure;}
function workerAnswer(result){return result?.answer?.trim()||(result?.status==="completed"?result.reason?.trim():"")||"";}
function reviewLabel(status){return {approved:"审核通过",changes_requested:"需要修改",unable_to_verify:"证据不足，暂不能确认"}[status]||status;}
function noTestNotice(text){return /(?:未配置|没有配置|没有可运行的).{0,20}(?:检查|测试)|configured checks.{0,12}(?:为空|empty)/i.test(text);}
function helpButton(topic,label="查看说明与处理方法"){
  const button=node("button",label,"help-link");button.type="button";button.addEventListener("click",()=>showHelp(topic));return button;
}
const HELP={
  checks:{title:"项目测试是什么？需要怎么确认？",lines:[
    "项目测试是运行已有的测试或构建命令，和上面“检查所选模型”的接口测试不同。没有运行项目测试，不等于任务失败，也不能算测试通过。",
    "如果只是要求模型回答问题或回复 OK：直接对照“目标”和“执行模型的回答”，再看原规划端的审核结论，不必额外配置测试命令。",
    "如果任务修改了文件：点击下方查看代码差异和测试输出。可以让原规划端按项目已有说明确认运行方式，并给出具体的命令、预期结果和检查对象。不要照抄其他项目的命令。",
    "核对后怎么结束：把实际结果发回原规划端，让它审核并保存核对结论。它明确确认已解决或与本次无关的提醒，会移到结果栏“已核对的提醒”，不再计入问题数；不是点一下就把失败测试变成通过。",
    "目前界面没有项目测试命令编辑器。高级用户可修改启动面板所用配置文件的 checks，格式为检查名称对应命令参数数组，供新任务使用；不要改写历史任务快照。也可以让原规划端帮助配置。已有任务的测试记录不会因此补成“已通过”。"
  ]},
  review:{title:"谁来审核执行结果？",lines:[
    "审核会核对用户目标、原方案、执行回答和已有测试证据。你可以交给当前 Codex 对话，或选择已保存的模型，不需要再配置接口与密钥。",
    "执行结束后，先看执行模型的回答。你可以点“我已确认，结束任务”自行确认；这会保存你的确认，不会启动模型，也不会把未运行的项目测试改成通过。",
    "选择审核方时，默认允许不通过后自动返修：审核列出问题，原执行模型修改、运行已有项目测试，再交回同一审核方复审，可能产生多轮费用。取消勾选则只保存审核结论。",
    "返修区可以申请完成当前阶段后暂停。证据不足不会自动猜测修改；相同问题且没有进展会暂停。可查看每轮问题、修改回答和复审结论，再决定继续。",
    "选择当前 Codex 时，把复制的内容发到当前对话。网页不能自行唤醒对话；保存审核后面板会更新。旧任务不会自动补发付费审核请求。"
  ]},
  residual:{title:"模型提醒怎么处理？",lines:[
    "这里保留执行模型报告的未处理事项，不代表每一条都是本次任务失败。请先对照用户目标与原方案，看它是否属于本次要求。",
    "例如只测试接力能否回答 OK，模型提到项目里原有的计算错误，需要在审核中说明是否属于本次范围，不能把它混成接口失败。",
    "如果涉及本次要求，交回原规划端指出具体文件、操作、实际现象和期望结果；让它给出可执行的处理办法。完整原话可在执行记录里查看。"
    ,"处理后由原规划端再次核对并明确记录哪条提醒已解决、依据是什么；该条会移到结果栏“已核对的提醒”，从问题数中移除。未处理的条目继续保留。API 模式可把具体问题与处理要求加入下一次任务，旧任务保留当时的问题记录。"
  ]},
  failure:{title:"根据失败位置排查",lines:[
    "先看问题中的失败阶段、模型、接口地址和上游状态码。连接失败时启动本地服务并核对端口；401/403 核对接口权限；429 说明上游限制，需要稍后再试或查看供应商说明。",
    "接口正常但任务失败时，点击查看执行记录里的公开错误与详细记录。将具体错误交给原规划端分析；不要仅凭本地 502 推断模型不可用。",
    "恢复任务前先确认有没有部分文件改动，避免重复实施。打开说明和查看记录都不会发送模型请求。"
  ]},
  progress:{title:"任务为什么变慢？",lines:[
    "这里的请求次数和请求大小来自本地转发记录，字节数不是 token 数。提醒只展示已有数据，不调用模型，也不会停止任务。",
    "如果模型反复调整同一产物、制作额外预览，核对这些步骤是否属于原任务。浏览器不可用时，应保留具体待验证项，交给当前 Codex 检查实际产物。",
    "工具定义、对话历史和图像都会增加输入。Relay 不设置每秒传输速率；第三方上游可能另有限流，不能由本地接口正常响应推断生成速度。",
    "失败后先查看项目已有文件，不要直接重做。旧任务保留原始配置；新的默认配置不再附加 2 MB 请求上限，显式配置的上限仍有效。"
  ]}
};
function showHelp(topic){const item=HELP[topic]||HELP.residual;$("issue-help-title").textContent=item.title;$("issue-help-content").replaceChildren(...item.lines.map(line=>node("p",line)));$("issue-help-dialog").showModal();}
function jumpTo(target){$("issue-help-dialog").close();$("history").open=true;if(target==="checks")document.querySelector(".verification").open=true;$(target).scrollIntoView({block:"start"});$(target).classList.add("jump-target");setTimeout(()=>$(target).classList.remove("jump-target"),2000);}
$("issue-help-close").addEventListener("click",()=>$("issue-help-dialog").close());
$("help-worker").addEventListener("click",()=>jumpTo("worker"));
$("help-checks").addEventListener("click",()=>jumpTo("checks"));
$("review-copy-close").addEventListener("click",()=>$("review-copy-dialog").close());
async function copyReview(data){
  const auto=data.auto_correct!==false;
  const request="请作为这个任务的"+(data.plan_source==="caller"?"原规划端":"审核端")+"审核执行结果，并将审核结论写回 Stage Relay 面板。由你审核，不再调用其他审核模型，也不要重复提交整个任务。\n"+
    "任务："+data.request+"\njob_id："+data.job_id+"\n结果版本："+data.review_revision+"\n记录目录："+data.path+"\n"+
    "读取该目录 plan.json、result.json、state.json 中的 validation 和 changes.patch，必要时核对项目文件。区分接力连通、实际任务目标与未运行的测试，不将执行端自报等同测试通过。\n"+
    "逐条核对 residual 提醒：说明具体检查对象、验证方法、预期结果；已解决或与本次范围无关的条目，在 evidence 写出依据，并将其从 0 开始的索引加入 resolved_residual，未解决的不加入。\n"+
    "若执行端没有可用浏览器而留下视觉验证提醒，请先检查本对话的浏览器集成；可用时打开实际产物验证构图、动画及提醒中的预期行为。不可用时保留未验证状态；不要自制替代渲染器或从 shell 启动浏览器。\n"+
    "不通过时在 change_requests 逐项填写 path、location、problem、requested_change、verification（含预期结果）；回复类任务的 path/location 可留空，不编造行号或命令。\n"+
    "调用 relay_record_review，传入 job_id、result_revision（上述结果版本）、auto_correct:"+auto+" 与 review：status 为 approved / changes_requested / unable_to_verify；summary 为给用户的结论，evidence 为依据数组，issues 为可操作的问题数组，resolved_residual 为已核对关闭的提醒索引数组。\n"+
    (auto?"我允许审核不通过后让原执行模型按意见返修（可能产生模型费用）。工具返回 queued 后使用 relay_wait 等待，读取新的结果版本、回答、测试与差异，并继续由你复审，直到通过、受阻、证据不足或暂停；不能复用旧结果版本，也不能替执行模型声称测试通过。网页无法在对话结束后自行唤醒你。\n":"本次只审核，不启动返修。\n")+
    "若工具未加载或不支持 change_requests/auto_correct，读取当前安装版接入配置 "+(data.review_record_wiring||"插件目录中的 .mcp.json")+"，使用其中 command 和 args 加 --record-review <保存上述参数的JSON文件> 写回。后续等待也沿用同一 command/args 加 --wait-job <job_id> --seconds 50 --full-result，取得新版结果与版本号；不要采用旧工具计算的结果版本。";
  $("review-copy-text").value=request;$("review-copy-dialog").showModal();
  try{await navigator.clipboard.writeText(request);$("review-copy-note").textContent="已复制，请粘贴回原 Codex 对话并发送。";}
  catch{$("review-copy-text").select();$("review-copy-note").textContent="请选中上方文字复制，发回原 Codex 对话。";}
}
function latestHandoff(data,predicate){for(let i=data.handoffs.length-1;i>=0;i--)if(predicate(data.handoffs[i]))return data.handoffs[i];return null;}
function workerResponse(data){
  // The worker's returned result is saved both as response.json and as a "-return"
  // handoff; prefer the newest handoff that actually carries a status field.
  const handoff=latestHandoff(data,h=>h.payload&&typeof h.payload.status==="string"&&/(^|-)worker(-return)?$/.test(h.phase||""));
  if(handoff)return {payload:handoff.payload,phase:(handoff.phase||"").replace(/-return$/,"")};
  const phase=[...data.phases].reverse().find(p=>p.role==="worker"&&p.status==="accepted"&&p.response&&p.response.status);
  return phase?{payload:phase.response,phase:phase.id}:null;}
function currentPhase(data){const pending=data.phases.find(p=>p.status==="running");
  if(pending)return pending;const phases=data.phases.slice().reverse();return phases.find(p=>p.status==="interrupted"||p.status==="failed")||phases[0]||null;}
function issue(label,tone,reason,evidence,next,extra){return {label,tone,reason,evidence,next,extra};}
function issuesFor(data){
  const rows=[];
  for(const check of data.checks||[])if(!checkPassed(check)){
    const evidence=[check.failure,check.stderr_tail,check.stdout_tail,
      check.stdout_path?"输出文件："+check.stdout_path:"",check.stderr_path?"错误文件："+check.stderr_path:""].filter(Boolean).join("\n");
    rows.push(issue("检查未通过","bad",check.failure||("独立检查以退出码 "+check.exit_code+" 结束"),evidence,""));
  }
  const found=workerResponse(data),result=found?.payload;
  const attemptLines=strings(result?.attempted).map(item=>"已尝试："+item);
  if(result?.status==="blocked")rows.push(issue("受阻","bad",result.blockedReason||"执行端报告阻塞",[...attemptLines,...strings(result.evidence)].join("\n"),result.decisionNeeded?("交回规划处理决策 "+result.decisionNeeded):""));
  if(result?.status==="needs_replan")rows.push(issue("需重新规划","bad",result.reason||"执行端要求调整方案",[...attemptLines,...strings(result.evidence)].join("\n"),result.decisionNeeded?("交回规划处理决策 "+result.decisionNeeded):""));
  if(data.state==="needs_manual_verification"&&data.checks?.length)
    rows.push(issue("待人工验证","warn","本地检查已有记录，但任务状态仍要求人工确认。","","查看下方独立检查与代码差异后确认。"));
  const unfinished=data.phases.filter(p=>p.role==="worker"&&["failed","interrupted"].includes(p.status));
  for(const phase of unfinished)if(!(data.failure&&data.failure.phase===phase.id))
    rows.push(issue("阶段未完成","bad",phase.failure||("执行阶段 "+phase.id+" 未正常结束"),"",["awaiting_recovery","awaiting_plan","interrupted"].includes(data.state)?"检查部分改动后用恢复流程继续":"",phase.id));
  if(data.state==="exhausted")rows.push(issue("达到重试上限","bad","已达到配置的修复或调整方案次数，任务未完成。","","检查未通过的检查输出和代码差异，处理后显式恢复。",""));
  if(data.state==="awaiting_plan")rows.push(issue("等待调整方案","warn","执行端要求调整方案，需要调用方给出修订后的计划。","","按受阻步骤与决策提交修订方案。",result?.failedStep||""));
  const resolved=new Set([...(data.review&&!data.review.stale?data.review.resolved_residual||[]:[]),...(data.confirmation&&!data.confirmation.stale?data.confirmation.acknowledged_residual||[]:[])]);
  for(const [index,item] of (result?.residual||[]).entries())if(!noTestNotice(item)&&!resolved.has(index))rows.push(issue("待核对的提醒","warn",item,"","点击“交回原规划端核对”，获得具体核对步骤；有依据确认后移入已核对记录。"));
  if(data.review&&!data.review.stale&&data.review.status!=="approved"){
    const review=data.review;rows.push(issue(review.status==="changes_requested"?"审核发现问题":"审核待确认",review.status==="changes_requested"?"bad":"warn",review.summary,strings(review.evidence).join("\n"),strings(review.issues).join("\n")));
  }
  return rows;
}
function renderIssues(data){
  const host=$("issues");host.replaceChildren();
  const rows=issuesFor(data), total=rows.length+(data.failure?1:0);
  $("issue-count").textContent=String(total);
  const bad=data.failure||rows.some(r=>r.tone==="bad");
  $("issue-count").className="badge "+(bad?"bad":rows.some(r=>r.tone==="warn")?"warn":"good");
  if(!rows.length){
    if(!data.failure){
      const verified=data.state==="completed"&&(data.checks||[]).length&&data.checks.every(checkPassed);
      host.append(node("p",verified?"暂无失败、阻塞或遗留事项；独立检查全部通过。":"暂无失败、阻塞或遗留事项。","empty"));
    }
    return;
  }
  for(const row of rows){
    const box=node("div",undefined,"issue "+row.tone);
    const head=node("div",undefined,"issue-head");
    head.append(node("span",row.label,"issue-label"),node("span",row.extra||"","issue-source"));
    box.append(head);
    if(row.reason)box.append(node("p",row.reason,"issue-reason"));
    if(row.evidence){const evidence=node("details",undefined,"issue-evidence");evidence.append(node("summary","证据"),node("pre",row.evidence));box.append(evidence);}
    if(row.next)box.append(node("p","下一步："+row.next,"issue-next"));
    box.append(helpButton(row.label.startsWith("审核")?"review":row.label.includes("检查")||row.label==="待人工验证"?"checks":row.label==="待核对的提醒"?"residual":"failure"));
    if(data.plan_source==="caller"&&data.job_id&&!data.failure&&["completed","needs_manual_verification","exhausted"].includes(data.state)){
      const action=node("button","交回原规划端核对","help-link");action.type="button";action.addEventListener("click",()=>copyReview(data));box.append(action);
    }
    host.append(box);
  }
}
function renderResult(data){
  const host=$("result");host.replaceChildren();
  const found=workerResponse(data),result=found?.payload;
  const phase=data.phases.find(p=>p.id===found?.phase)||null;
  if(!result){
    const state=data.state;
    host.append(node("p",state==="running"?"执行端仍在运行，尚未交付结果。":state==="ready"?"等待执行端交付结果。":"尚未记录执行端结果。","empty"));
    if(data.phases.some(p=>p.role==="planner"&&p.response?.goal)){const plan=node("div",undefined,"result-line");plan.append(node("strong","规划目标："),node("span",data.phases.filter(p=>p.role==="planner").at(-1).response.goal));host.append(plan);}
    return;
  }
  const latestWorker=[...data.phases].reverse().find(p=>p.role==="worker");
  if(latestWorker&&["failed","interrupted"].includes(latestWorker.status)&&latestWorker.id!==found.phase)
    host.append(node("p","最近一次执行阶段（"+latestWorker.id+"）未正常结束，下面是更早一次已接纳的执行结果。","prior-round-note"));
  const stateRow=node("div",undefined,"result-line result-state");
  stateRow.append(node("span",reportText(result.status),"badge "+(result.status==="completed"?"good":result.status==="blocked"?"bad":"warn")));
  if(found.phase)stateRow.append(node("span",found.phase+" · "+(phase?labels[phase.status]||phase.status:""),"result-phase"));
  host.append(stateRow);
  const answer=node("section",undefined,"worker-answer");answer.append(node("h3","执行模型的回答"));
  const reply=workerAnswer(result);answer.append(node("div",reply||"这条记录没有保存给用户的回答。可查看执行记录中的公开消息与证据。",reply?"answer-text":"empty"));
  if(!result.answer&&reply)answer.append(node("small","来自这条旧记录的完成说明。","hint"));
  const confirmed=data.confirmation&&!data.confirmation.stale;
  if(confirmed){answer.append(node("p","✓ 你已确认审核完毕，流程已结束。","user-confirmed"),node("small",new Date(data.confirmation.time*1000).toLocaleString()+" · 由你自行确认","hint"));}
  else if(data.can_confirm){
    const actions=node("div",undefined,"result-actions"),accept=node("button","我已确认，结束任务","confirm-result");accept.type="button";
    accept.addEventListener("click",()=>confirmExecution(data,accept));actions.append(accept);
    const handoff=node("button","选择审核方式");handoff.type="button";handoff.addEventListener("click",()=>openReviewChoice(data));actions.append(handoff);
    answer.append(actions,node("p","可自行确认结束，或选择当前 Codex / 已保存的模型审核。自行确认不会发送模型请求。","hint"));
  }
  host.append(answer);
  const reviewBox=node("section",undefined,"planner-review");reviewBox.append(node("h3","审核结论"));
  const review=data.review;
  if(review&&!review.stale){
    reviewBox.append(node("span",reviewLabel(review.status),"badge "+(review.status==="approved"?"good":review.status==="changes_requested"?"bad":"warn")),node("p",review.summary,"answer-text"),node("small","审核方："+(review.reviewer_selection?.provider_name?review.reviewer_selection.provider_name+" · ":"")+review.reviewer,"hint"));
    if(review.evidence?.length)reviewBox.append(detail("审核依据",review.evidence,"review-evidence"));
    for(const item of review.change_requests||[])reviewBox.append(changeRequestCard(item));
  }else{
    const reviewing=data.phases.some(p=>p.role==="reviewer"&&p.status==="running");
    reviewBox.append(node("p",confirmed?"本次由你自行确认结束。":review?.stale?"执行结果已变化，原审核不再适用。":reviewing?"所选审核模型正在核对执行结果…":"你可以自行确认，或选择当前 Codex / 已保存的模型审核。","hint"));
  }
  reviewBox.append(helpButton("review","审核方式说明"));host.append(reviewBox);
  const resolved=[...new Set([...(review&&!review.stale?review.resolved_residual||[]:[]),...(confirmed?data.confirmation.acknowledged_residual||[]:[])])];
  if(resolved.length){const box=node("details",undefined,"resolved-notices");box.dataset.key="resolved-notices";box.append(node("summary",(confirmed?"你已确认的提醒（":"已核对的提醒（")+resolved.length+"）"));for(const index of resolved)if(result.residual?.[index])box.append(node("p",result.residual[index]));box.append(node("p",confirmed?"你已确认这些提醒并结束流程，原始记录保留在这里。":"核对依据见上方原规划端审核；这些提醒不再计入当前问题。","hint"));host.append(box);}
  if(result.status==="blocked"){const line=node("p",undefined,"result-reason");line.append(node("strong","阻塞原因："),node("span",result.blockedReason||"执行端未记录阻塞原因"));host.append(line);}
  else if(result.status==="needs_replan"){const line=node("p",undefined,"result-reason");line.append(node("strong","调整原因："),node("span",result.reason||"执行端未记录调整原因"));host.append(line);}
  const checks=data.checks||[];
  if(checks.length){
    const box=node("div",undefined,"verify-summary");
    box.append(node("strong","项目自动测试（独立检查）"),node("span",checks.every(checkPassed)?"全部通过":"存在未通过项","badge "+(checks.every(checkPassed)?"good":"bad")));
    for(const check of checks)box.append(node("div",(check.id||"检查")+" · "+(checkPassed(check)?"通过":"未通过")+(check.failure?" · "+check.failure:""),"verify-row"));
    host.append(box);
  }else if(result.status==="completed"){const box=node("div",undefined,"verify-summary");box.append(node("p",data.configured_checks?.length?"项目测试尚未完成，请查看运行状态和测试记录。":"未运行项目自动测试；本次没有配置测试命令。这不是接口故障。"),helpButton("checks","这对本次任务意味着什么？"));host.append(box);}
  else host.append(node("p","独立检查：未运行（执行端未报告完成）。","verify-summary warn-text"));
  const changed=strings(result.changed);
  if(changed.length){host.append(node("strong","实际改动"));const list=node("ul");for(const item of changed)list.append(node("li",item));host.append(list);}
  else if(result.status==="completed")host.append(node("p","执行端未报告实际改动。","empty"));
  const selfChecks=strings(result.checks);
  if(selfChecks.length){host.append(node("strong","执行端自报检查"));const list=node("ul");for(const item of selfChecks)list.append(node("li",item));host.append(list);}
}
async function confirmExecution(data,button){
  button.disabled=true;
  try{await localPost("api/confirm",{job_id:data.job_id,result_revision:data.review_revision});$("history-message").textContent="你已确认当前回答，流程已结束。已保存你的确认；项目测试仍按实际运行记录显示。";snapshotKey="";runsKey="";await refresh(true);}
  catch(error){$("history-message").textContent="确认未保存："+error.message;button.disabled=false;}
}
async function openReviewChoice(data){
  if(reviewBusy)return;
  reviewChoice=data;reviewChoices=[];
  $("review-choice-task").textContent="任务："+(data.title||data.request);
  $("review-choice-note").textContent="正在读取已保存模型…";
  $("review-saved-model").replaceChildren(node("option","正在读取…"));$("review-use-model").disabled=true;
  $("review-choice-dialog").showModal();
  try{
    const view=await api("api/launch");if(reviewChoice!==data)return;
    const select=$("review-saved-model");select.replaceChildren(Object.assign(node("option","请选择已有模型"),{value:""}));
    for(const profile of view.profiles||[]){const group=node("optgroup");group.label=profile.name;
      for(const model of profile.models){const index=reviewChoices.push({provider_id:profile.id,model})-1;group.append(Object.assign(node("option",profile.name+" · "+model),{value:String(index)}));}if(group.children.length)select.append(group);}
    $("review-choice-note").textContent=reviewChoices.length?"已读取 "+reviewChoices.length+" 个已保存模型。尚未发送模型请求。":"还没有已保存模型，可直接选择当前 Codex 审核。";
  }catch(error){$("review-choice-note").textContent="未能读取已有模型："+error.message+"；仍可交给 Codex。";}
}
$("review-choice-close").addEventListener("click",()=>{reviewChoice=null;$("review-choice-dialog").close();});
$("review-saved-model").addEventListener("change",()=>{$("review-use-model").disabled=reviewBusy||$("review-saved-model").value==="";});
$("review-use-codex").addEventListener("click",()=>{const data=reviewChoice;if(!data||reviewBusy)return;reviewChoice=null;$("review-choice-dialog").close();copyReview({...data,auto_correct:$("review-auto-correct").checked});});
$("review-use-model").addEventListener("click",async()=>{
  const data=reviewChoice,choice=reviewChoices[$("review-saved-model").value];if(!data||!choice||reviewBusy)return;
  reviewBusy=true;$("review-use-model").disabled=true;$("review-use-codex").disabled=true;
  try{const value=await localPost("api/review",{job_id:data.job_id,result_revision:data.review_revision,...choice,auto_correct:$("review-auto-correct").checked});if(value.status==="queued")pendingJob=data.job_id;snapshotKey="";$("history-message").textContent=value.reused?"已读取所选模型对这份结果的已有审核。":"已交给 "+choice.model+" 审核；是否自动返修按本次选择执行。";reviewChoice=null;$("review-choice-dialog").close();await refresh(true);}
  catch(error){$("review-choice-note").textContent="未能发起审核："+error.message;}
  finally{reviewBusy=false;$("review-use-codex").disabled=false;$("review-use-model").disabled=$("review-saved-model").value==="";}
});
function renderProgress(data){
  const box=$("progress");const live=["running","ready","queued"].includes(data.state)||data.phases.some(p=>p.status==="running");
  const activity=data.request_activity;
  box.hidden=!live&&!activity?.warnings?.length;if(box.hidden)return;
  const phase=currentPhase(data);
  $("progress-stage").replaceWith(Object.assign(badge(phase?phase.status:data.state),{id:"progress-stage"}));
  const host=$("progress-event");host.replaceChildren();
  if(activity){
    let metrics="累计模型请求："+activity.forwarded_requests+" 次";
    if(Number.isFinite(activity.last_request_bytes))metrics+=" · 最近请求："+(activity.last_request_bytes/1024).toFixed(1)+" KiB";
    if(Number.isFinite(activity.first_request_bytes))metrics+=" · 首次请求："+(activity.first_request_bytes/1024).toFixed(1)+" KiB";
    host.append(node("p",metrics,"progress-line"));
    for(const text of activity.warnings||[])host.append(node("p",text,"progress-warning"));
    if(activity.warnings?.length)host.append(helpButton("progress","查看为何变慢与处理方法"));
  }
  if(!phase){host.append(node("p","等待本地进程启动，尚无阶段记录。","empty"));return;}
  host.append(node("p","当前阶段："+(roleNames[phase.role]||phase.role)+" · "+phase.id+(phase.status==="running"?" · 检查点显示运行中，进程存活未独立确认":""),"progress-line"));
  const event=(phase.events||[]).at(-1);
  if(event){const preview=event.kind==="command"?"命令："+event.text:event.kind==="files"?"文件修改："+event.text:"公开消息："+event.text;
    host.append(node("pre",preview,"progress-event-text"));}
  else host.append(node("p","暂未记录公开消息或工具事件。","empty"));
  const browser=(phase.events||[]).filter(e=>e.kind==="capability").at(-1);
  if(browser?.browser_available===false)host.append(node("p",browser.text+"完成后选择当前 Codex 审核，不能把源码检查当作动画已验证。","progress-warning"));
}
function renderFailure(failure, host=$("failure")) {
  host.replaceChildren();host.hidden=!failure;if(!failure)return;
  host.append(node("h3",failure.message||"任务未能完成"));
  const names={stage:"失败阶段",role:"角色",model:"模型",endpoint:"接口",upstream_status:"上游 HTTP 状态",local_status:"本地 HTTP 状态",body_bytes:"本次请求大小（字节）",limit_bytes:"已配置上限（字节）",elapsed_seconds:"阶段已运行（秒）",idle_seconds:"连续无活动（秒）",timeout_seconds:"超时阈值（秒）",path:"位置",executable:"程序",exception:"异常类型",errno:"系统错误码",winerror:"Windows 错误码",code:"错误代码"};
  for(const [key,label] of Object.entries(names))if(failure[key]!==undefined)host.append(node("div",label+"："+(key==="role"?(roleNames[failure[key]]||failure[key]):failure[key]),"diagnostic-line"));
  host.append(node("p","处理建议："+(failure.action||"旧记录未保存详细原因，请查看阶段公开错误和本地日志。")));
  if(failure.logs?.length)host.append(node("div","本地记录："+failure.logs.join("\n"),"diagnostic-line"));
  host.append(helpButton("failure"));
}
const KIND_LABELS={catalog:"查看模型列表",generation:"试着回答一句",compat:"试跑接力格式"};
let checkBusy=false;
function renderChecks(data){
  const host=$("check-results");host.replaceChildren();
  const rows=data?.records||[];
  $("panel-version").textContent=data?.version?("面板 "+data.version+" · 若插件已更新请重新连接"):"";
  if(!rows.length){host.append(node("p","还没有检查记录。三种检查的范围不同，可分别点击上方按钮。","empty"));return;}
  for(const [index,record] of rows.slice(0,6).entries()){
    const passed=record.status==="passed"&&record.valid!==false;
    const row=node("article",undefined,"check-record "+(passed?"good":"bad"));
    const side=node("time",undefined,"check-when"),box=node("div",undefined,"check-record-main");
    const date=typeof record.time==="number"?new Date(record.time*1000):null;
    if(index===0)side.append(node("span","最近一次","check-latest"));
    side.append(node("span","检查时间","check-time-label"));
    if(date&&!Number.isNaN(date.getTime())){
      side.dateTime=date.toISOString();
      side.append(node("strong",date.toLocaleTimeString("zh-CN",{hour12:false,hour:"2-digit",minute:"2-digit",second:"2-digit"}),"check-clock"),
                  node("span",date.toLocaleDateString("zh-CN",{year:"numeric",month:"2-digit",day:"2-digit"}),"check-date"));
    }else side.append(node("strong","时间未知","check-date"));
    if(record.elapsed_seconds!==null&&record.elapsed_seconds!==undefined)side.append(node("span","耗时 "+record.elapsed_seconds+" 秒","check-duration"));
    row.append(side,box);
    const head=node("div",undefined,"check-record-head");
    head.append(node("span",KIND_LABELS[record.kind]||record.kind,"check-kind"),
                node("span",record.status==="passed"?"通过":"未通过","badge "+(record.status==="passed"?"good":"bad")),
                node("span",record.valid===false?"记录已失效":"记录有效","check-valid"));
    box.append(head);
    box.append(node("p",(record.provider_name||"—")+" · "+record.model+" · "+(record.http_status!==null&&record.http_status!==undefined?"HTTP "+record.http_status:"无 HTTP 状态")+" · 发起请求 "+number(record.model_calls),"check-meta"));
    if(record.endpoint)box.append(node("p","接口："+record.endpoint,"check-meta"));
    if(record.stage)box.append(node("p","失败/完成阶段："+record.stage,"check-meta"));
    if(record.kind!=="catalog")box.append(node("p","计费提示：本次检查发送了模型请求，可能产生费用。","check-cost"));
    box.append(node("p",record.message||"","check-message"));
    if(record.answer)box.append(node("pre",record.answer,"check-answer"));
    if(record.action)box.append(node("p","说明："+record.action,"check-action"));
    if(record.valid===false)box.append(node("p",record.invalid_reason,"check-action"));
    host.append(row);
  }
  host.append(node("p",data.note||"历史通过只说明测试当时成功。","hint"));
}
async function loadChecks(){
  try{renderChecks(await api("api/checks"));}
  catch(error){$("check-note").textContent="读取检查记录失败："+error.message;}
}
async function runCheck(kind){
  if(checkBusy)return;
  const provider=$("launch-worker-provider").value,model=$("launch-worker-model").value;
  if(!provider||!model){$("check-note").textContent="请先选择执行模型接口和模型。";return;}
  if(kind!=="catalog"){
    const label=KIND_LABELS[kind]||kind;
    if(!window.confirm(label+" 会通过真实 Codex 向所选接口发送一次模型请求，\n可能产生费用（费用未知，不保证免费）。\n\n不会创建任务记录，也不会修改你的项目。\n\n继续运行？")){
      $("check-note").textContent=label+" 已取消；没有发送任何请求。";return;
    }
  }
  checkBusy=true;for(const id of ["check-catalog","check-generation","check-compat"])$(id).disabled=true;
  $("check-note").textContent=(KIND_LABELS[kind]||kind)+" 进行中…"+(kind==="catalog"?"（只读取模型目录，不发起生成，不计费）":"（已发送一次模型请求，可能产生费用）");
  try{
    const result=await localPost("api/check",{provider_id:provider,model,kind});
    $("check-note").textContent=(KIND_LABELS[kind]||kind)+"："+(result.status==="passed"?"通过":"未通过")+" · "+(result.message||"");
    await loadChecks();
  }catch(error){$("check-note").textContent="检查未完成："+error.message;}
  finally{checkBusy=false;for(const id of ["check-catalog","check-generation","check-compat"])$(id).disabled=false;}
}
$("check-catalog").addEventListener("click",()=>runCheck("catalog"));
$("check-generation").addEventListener("click",()=>runCheck("generation"));
$("check-compat").addEventListener("click",()=>runCheck("compat"));
$("check-refresh").addEventListener("click",loadChecks);
async function api(path) {
  const response = await fetch(new URL(path, document.baseURI), {cache:"no-store"});
  if (!response.ok) {const error=await response.json().catch(()=>({}));throw new Error(error.error||"本地文件暂不可读（"+response.status+"）");}
  return response.json();
}
function renderRole(role, data) {
  const host=$(role), previous=host.querySelector(".output");
  if(previous) outputScroll[role] = {top:previous.scrollTop, bottom:previous.scrollHeight-previous.scrollTop-previous.clientHeight<40};
  host.replaceChildren();
  const phases=data.phases.filter(p=>p.role===role||(role==="planner"&&p.role==="reviewer")), spec=data.roles[role];
  if(!phases.some(p=>p.id===phaseChoice[role])) delete phaseChoice[role];
  const phase=phases.find(p=>p.id===phaseChoice[role])||phases.at(-1);
  const head=node("div",undefined,"agent-head"), identity=node("div",undefined,"identity"), title=node("h2");
  title.append(node("span",roleNames[role]));
  identity.append(title,phase?badge(phase.status):node("span",spec.reused?"已复用方案":role==="worker"?"等待计划":"等待启动","badge"));
  const phaseSpec=phase?.role==="reviewer"?(data.roles.reviewer||spec):spec;
  head.append(identity,node("div",phase?.model||phaseSpec.model,"model"));
  const metrics=node("div",undefined,"role-meta");
  let output=0, hasUsage=false;
  for(const p of phases) for(const u of p.usage||[]) if(u&&typeof u.output_tokens==="number"){output+=u.output_tokens;hasUsage=true;}
  metrics.append(node("span","请求 "+number(phaseSpec.requests)+(phaseSpec.max_requests?" / "+phaseSpec.max_requests:"")),node("span","输出 token "+(hasUsage?number(output):"未知")));
  head.append(metrics);
  if(phases.length){const sel=node("select",undefined,"phase-picker");sel.setAttribute("aria-label",roleNames[role]+"阶段记录");for(const p of phases){const o=node("option",p.id+" · "+(labels[p.status]||p.status));o.value=p.id;sel.append(o);}sel.value=phase?.id;sel.addEventListener("change",()=>{if(sel.value===phases.at(-1)?.id)delete phaseChoice[role];else phaseChoice[role]=sel.value;outputScroll[role]={top:0,bottom:true};renderRole(role,data);});head.append(sel);}
  host.append(head);
  const work=node("div",undefined,"work");work.append(node("div","本阶段任务","work-label"));
  if(role==="planner")work.append(node("div",phase?.role==="reviewer"?"由所选审核模型核对任务目标、执行回答、文件和检查证据，给出审核结论。":spec.reused?"方案由调用方 agent 提供，未启动额外规划模型。":phase?.id!==phases[0]?.id?"处理执行端反馈，修订具体决策和实施计划。":"读取相关代码，产出实施计划与验收条件。"));
  else { const h=data.handoffs.find(h=>h.phase===phase?.id), plan=h?.payload?.plan; if(plan?.steps?.length){
      const ol=node("ol");for(const s of plan.steps)ol.append(node("li",s.id+" · "+s.do));
      if(plan.steps.length>6){const steps=node("details",undefined,"nested-details");steps.append(node("summary","实施步骤 "+plan.steps.length+" 项（点击查看）"),ol);work.append(steps);}
      else work.append(ol);
    }else work.append(node("div","等待规划 agent 的计划。")); }
  host.append(work);
  const outputBox=node("div",undefined,"output");
  const result=phase?.response||(spec.reused?data.handoffs.find(h=>h.to==="worker")?.payload?.plan:null);
  if(result&&Object.keys(result).length){const preview=node("div",undefined,"event");preview.append(node("div",role==="planner"?"规划输出":"执行反馈","event-title"));readable(preview,result,{stepsInDetails:(result.steps||[]).length>6});outputBox.append(preview);}
  if(!phase?.events?.length)outputBox.append(node("div",phase?"暂未记录公开消息或工具事件；已保存的结果显示在上方。":"此角色尚未启动，等待期间不产生模型请求。","empty"));
  for(const [index,e] of (phase?.events||[]).entries()){
    if(e.kind==="command"||e.kind==="files"){outputBox.append(detail((e.kind==="command"?"命令":"文件修改")+(e.status?" · "+e.status:""),[e.text,e.output].filter(Boolean).join("\n"),phase.id+"-event-"+index));continue;}
    if(e.kind==="message"){try{const parsed=JSON.parse(e.text);if(parsed&&typeof parsed==="object"&&(parsed.goal||(parsed.status&&parsed.changed)))continue;}catch{}}
    const entry=node("div",undefined,"event "+e.kind);entry.append(node("div",e.kind==="error"?"错误":"公开消息","event-title"),node("pre",e.text));outputBox.append(entry);
  }
  host.append(outputBox);
  if(phase){const advanced=node("details");advanced.dataset.key=phase.id+"-advanced";advanced.append(node("summary","详细记录"),detail("实际输入",phase.input,phase.id+"-input"),detail("结构化结果",phase.response,phase.id+"-result"),detail("Token 记录（CLI 报告，输入含缓存）",phase.usage,phase.id+"-usage"));if(phase.failure||phase.stderr)advanced.append(detail("进程诊断",[phase.failure,phase.stderr].filter(Boolean).join("\n"),phase.id+"-diagnostic"));host.append(advanced);}
  const scroll=outputScroll[role];if(!scroll&&phase?.status!=="running")outputBox.scrollTop=0;else if(scroll?.bottom!==false)outputBox.scrollTop=outputBox.scrollHeight;else outputBox.scrollTop=scroll.top;
}
function handoffPurpose(h){
  const payload=h.payload||{};
  if(payload.plan&&payload.plan.goal)return payload.plan.goal;
  if(payload.goal)return payload.goal;
  if(payload.status)return "交付结果："+reportText(payload.status);
  if(payload.request)return payload.request;
  return "";
}
function renderHandoffs(data){
  $("handoffs").replaceChildren();
  for(const [index,h] of data.handoffs.entries()){
    const row=node("div",undefined,"handoff"), card=node("div",undefined,"handoff-card"), top=node("div",undefined,"handoff-top"), text=node("div");
    text.append(node("div",h.title,"handoff-title"));
    top.append(text,badge(h.status));card.append(top);
    const purpose=handoffPurpose(h);
    if(purpose){const line=node("p",undefined,"handoff-purpose");line.append(node("strong","交接目的："),node("span",purpose));card.append(line);}
    const body=node("details",undefined,"handoff-details");body.dataset.key="handoff-"+h.phase;
    body.append(node("summary","详情：目标、步骤、代码事实、完整输入与原始 JSON"));
    body.append(node("div",h.evidence+(h.evidence.endsWith("/response.json")?" · 已保存发送方结果":" · 已保存接收阶段实际输入"),"handoff-proof"));
    const inner=node("div",undefined,"handoff-body");readable(inner,h.payload,{stepsInDetails:true,skipStatus:!(h.payload&&h.payload.status)});
    body.append(inner,detail("原始交接包",h.payload,"handoff-raw-"+h.phase));card.append(body);
    row.append(node("div",String(index+1).padStart(2,"0"),"handoff-number"),card);$("handoffs").append(row);
  }
  if(!data.handoffs.length)$("handoffs").append(node("p","接收阶段开始后，这里会显示实际交接输入。","empty"));
}
function changeRequestCard(item){
  const box=node("div",undefined,"review-change");
  box.append(node("strong",(item.path||"回答 / 任务结果")+(item.location?" · "+item.location:"")));
  for(const [label,key] of [["问题","problem"],["修改要求","requested_change"],["如何验证","verification"]])if(item[key])box.append(node("p",label+"："+item[key]));
  return box;
}
function renderCorrections(data){
  const rounds=data.correction_history||[],cycle=data.review_cycle||{};
  const rejected=data.review&&!data.review.stale&&data.review.status==="changes_requested";
  $("corrections").hidden=!rounds.length&&!cycle.active&&!rejected;
  $("correction-actions").replaceChildren();$("correction-rounds").replaceChildren();
  const states={correcting:"原执行模型正在按审核意见修改",awaiting_caller:"返修已完成，等待当前 Codex 复审",reviewing:"等待同一审核方复审",paused_no_progress:"相同问题再次出现，结果未见变化，已暂停返修",paused_by_user:"已按你的要求暂停",approved:"审核通过，返修结束",unable_to_verify:"证据不足，已停止自动返修",changes_requested:"审核要求修改",user_confirmed:"你已自行确认，返修流程结束"};
  $("correction-status").textContent=(states[data.review_state]||"按审核意见继续处理")+(data.pause_requested&&cycle.active?" · 已申请在当前阶段后暂停":"");
  function action(text,route,payload){const button=node("button",text);button.type="button";button.addEventListener("click",async()=>{button.disabled=true;try{const result=await localPost(route,payload);if(result.status==="queued")pendingJob=data.job_id;snapshotKey="";await refresh(true);}catch(error){$("history-message").textContent=error.message;button.disabled=false;}});$("correction-actions").append(button);return button;}
  if(cycle.active&&data.review_state!=="awaiting_caller"&&data.job_id)action("完成当前阶段后暂停返修","api/pause-corrections",{job_id:data.job_id}).disabled=!!data.pause_requested;
  if(data.review_state==="awaiting_caller"&&data.can_confirm){const button=node("button","把新结果交回当前 Codex 复审");button.type="button";button.addEventListener("click",()=>copyReview({...data,auto_correct:cycle.auto_correct!==false}));$("correction-actions").append(button);}
  if((data.state==="review_paused"||rejected&&!cycle.active)&&data.can_confirm&&!(data.confirmation&&!data.confirmation.stale))action("继续按审核意见修改","api/correct",{job_id:data.job_id,result_revision:data.review_revision});
  for(const round of [...rounds].reverse()){
    const card=node("details",undefined,"correction-round");card.dataset.key="correction-"+round.round;
    const names={queued:"等待修改",checking:"修改已返回，核对中",awaiting_review:"等待复审",awaiting_caller:"等待当前 Codex 复审",approved:"复审通过",changes_requested:"需继续修改",unable_to_verify:"证据不足",paused_no_progress:"无进展，已暂停",paused_by_user:"已暂停",user_confirmed:"用户已确认结束"};
    card.append(node("summary","第 "+round.round+" 轮 · "+(names[round.status]||round.status)));
    card.append(node("h3","审核指出的问题"),node("p",round.review.summary));
    if(round.review.change_requests?.length)for(const item of round.review.change_requests)card.append(changeRequestCard(item));
    else for(const text of round.review.issues||[])card.append(node("p",text));
    if(round.answer)card.append(node("h3","执行模型的修改回答"),node("pre",round.answer));
    if(round.checks?.length)card.append(node("p","项目检查："+round.checks.map(c=>c.id+" "+(checkPassed(c)?"通过":"未通过")).join("；")));
    if(round.final_review)card.append(node("h3","本轮复审结论"),node("p",reviewLabel(round.final_review.status)+" · "+round.final_review.summary));
    $("correction-rounds").append(card);
  }
}
function renderSubagents(data){
  const phases=(data.phases||[]).filter(p=>p.role==="worker");
  const rows=phases.flatMap(p=>(p.subagents?.agents||[]).map(a=>({...a,phase:p.id,phaseStatus:p.status,usage:p.routed_usage?.agents?.[a.id]})));
  $("subagents-results").hidden=!rows.length;$("subagents-cards").replaceChildren();
  const reports=phases.map(p=>p.routed_usage).filter(u=>u?.requests);
  const complete=reports.length&&reports.every(u=>u.complete);
  $("subagents-usage").textContent="执行端与子任务共转发 "+reports.reduce((n,u)=>n+u.requests,0)+" 次请求 · "+(complete?"上游报告输入 "+reports.reduce((n,u)=>n+u.input_tokens,0)+" / 输出 "+reports.reduce((n,u)=>n+u.output_tokens,0)+" token":"上游用量不完整，合计未知")+"。这份用量包含执行主模型，不与 CLI 用量相加；费用未知。";
  const states={pending_init:"准备中",running:"执行中",completed:"已返回",errored:"执行失败",interrupted:"已中断",shutdown:"已关闭",not_found:"未找到",unknown:"状态待确认"};
  for(const a of rows){
    const unfinished=!a.closed&&a.phaseStatus&&a.phaseStatus!=="running";
    const card=node("article",undefined,"subagent-card"+(["errored","interrupted","not_found"].includes(a.status)||unfinished?" has-problem":""));
    card.append(node("h3",(states[a.status]||a.status)+(a.closed?" · 已收尾":" · 尚未确认收尾")),node("p",a.model||"沿用执行模型","hint"));
    card.append(node("p",(a.goal||"未记录子任务目标").slice(0,140)+(a.goal?.length>140?"…":"")));
    if(unfinished)card.append(node("p","执行阶段已停止，请核对子任务结果与已有改动后再恢复。","form-error"));
    const goal=node("details");goal.append(node("summary","子任务指示"),node("pre",a.goal||"未记录指示"));
    card.append(goal,node("h4","子任务回答"),node("pre",a.answer||"尚未收到回答"));
    if(a.usage?.complete)card.append(node("p","输入 "+a.usage.input_tokens+" / 输出 "+a.usage.output_tokens+" token","hint"));
    else card.append(node("p","单个子任务用量：接口未提供可归属的完整记录。","hint"));
    card.append(node("small",a.phase+" · "+a.id));$("subagents-cards").append(card);
  }
}
function addDelegationTask(){
  const row=node("fieldset",undefined,"delegation-task");row.append(node("legend","子任务"));
  const goal=node("textarea");goal.rows=2;goal.placeholder="具体目标与预期结果";goal.dataset.field="goal";
  const access=node("select");access.dataset.field="access";for(const [value,text] of [["read","只读分析"],["write","允许修改指定文件"]]){const o=node("option",text);o.value=value;access.append(o);}
  const files=node("textarea");files.rows=2;files.placeholder="项目内文件路径，每行一个，例如 src/parser.py";files.dataset.field="files";
  for(const [text,input] of [["子任务目标",goal],["文件操作",access],["相关文件 / 独占写入范围",files]]){const label=node("label",text);label.append(input);row.append(label);}
  const remove=node("button","移除这项分工");remove.type="button";remove.addEventListener("click",()=>row.remove());row.append(remove);$("subagents-tasks").append(row);
}
function delegationSelection(){
  const value={mode:$("subagents-mode").value},concurrency=$("subagents-concurrency").value;
  if(concurrency){const n=Number(concurrency);if(!Number.isInteger(n)||n<1||n>100)throw new Error("同时运行数量请填 1 到 100 的整数。");value.max_concurrent=n;}
  if(value.mode==="manual"){
    value.tasks=[...$("subagents-tasks").children].map(row=>({goal:row.querySelector('[data-field="goal"]').value.trim(),access:row.querySelector('[data-field="access"]').value,files:row.querySelector('[data-field="files"]').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean)}));
    if(!value.tasks.length||value.tasks.some(t=>!t.goal||!t.files.length))throw new Error("请填写每项分工的目标与文件范围。");
  }
  return value;
}
$("subagents-concurrency").disabled=true;
$("subagents-mode").addEventListener("change",()=>{$("subagents-concurrency").disabled=$("subagents-mode").value==="off";$("subagents-manual").hidden=$("subagents-mode").value!=="manual";if(!$("subagents-manual").hidden&&!$("subagents-tasks").children.length)addDelegationTask();});
$("subagents-add").addEventListener("click",addDelegationTask);
function render(data) {
  const open=new Set([...document.querySelectorAll("details[open]")].map(d=>d.dataset.key).filter(Boolean));
  $("request").textContent=data.request||"尚未记录任务";$("project").textContent=data.project||"";
  document.title=(data.title||"协作面板")+" · Stage Relay";

  renderFailure(data.failure);
  if(data.failure)$("failure").prepend(node("div","所选任务记录 · "+(data.updated_at?new Date(data.updated_at*1000).toLocaleString():data.title),"hint"));
  $("task-state").replaceWith(Object.assign(badge(data.state),{id:"task-state"}));
  renderResult(data);renderIssues(data);renderProgress(data);renderSubagents(data);renderCorrections(data);
  for(const role of ["planner","worker"])renderRole(role,data);
  renderHandoffs(data);
  $("checks").replaceChildren();for(const check of data.checks||[]){const box=node("div",undefined,"check");box.append(node("strong",check.id+" · "+(check.exit_code===0&&!check.failure?"通过":"未通过")),node("pre",[check.failure,check.stdout_tail,check.stderr_tail].filter(Boolean).join("\n")));$("checks").append(box);}
  if(!data.checks?.length)$("checks").append(node("div","还没有独立检查记录。","empty"));
  $("patch").textContent=data.patch||"尚无已保存差异。差异由运行器在阶段结束时保存。";
  $("notice").className="notice"+(data.failure?" error":"");
  $("notice").textContent=data.failure?(data.failure.message||"任务未能完成，具体原因见上方诊断。"):"本地日志 · 运行中约 1.5 秒、空闲约 10 秒刷新 · 后台暂停读取";
  $("updated").textContent="本地读取 "+new Date(data.observed_at*1000).toLocaleTimeString();
  for(const el of document.querySelectorAll("details"))if(open.has(el.dataset.key))el.open=true;
}
async function refresh(force=false){
  const awaitingPlan=launchEnabled&&!pendingJob&&$("launch-mode").value==="caller"&&callerDraft&&!callerDraft.plan&&callerDraft.status!=="blocked";
  if(!force&&(document.hidden||(!pendingJob&&!currentActive&&!awaitingPlan&&Date.now()-lastAutomaticRefresh<10000)))return;
  if(busy)return;busy=true;
  lastAutomaticRefresh=Date.now();
  try{
    if(launchEnabled&&!pendingJob&&Date.now()-lastDraftCheck>=1000)await loadDraft(true);
    if(pendingJob){const job=await api("api/job/"+pendingJob);current=job.run_id;if(!job.active){$("launch-message").textContent=job.failure?(job.diagnostic?.message||"任务已停止，查看下方具体原因。"):job.status==="user_confirmed"?"你已确认，流程已结束。":"执行已结束。查看回答后，可自行确认结束，或交回原规划端审核。";pendingJob="";launchSignature="";launchKey="";repeatReady=true;listTick=0;launchMode();}else $("launch-message").textContent="任务已提交 · "+(labels[job.status]||job.status)+" · 正在读取交接记录…";}
    if(force||pendingJob||!currentActive||++listTick%4===1){const {runs,last_deleted,last_deleted_ids}=await api("api/runs"),select=$("run"),listKey=JSON.stringify(runs);lastDeleted=last_deleted||"";lastDeletedIds=last_deleted_ids||(lastDeleted?[lastDeleted]:[]);$("undo-delete").hidden=!lastDeletedIds.length;$("undo-delete").textContent="撤销上次删除（"+lastDeletedIds.length+" 条）";if(listKey!==runsKey){select.replaceChildren();for(const r of runs){const option=node("option",(r.mode==="mock"?"[模拟] ":"")+(r.title||r.name)+" · "+(labels[r.status]||r.status));option.value=r.id;option.title=r.name;select.append(option);}renderHistoryChoices(runs);runsKey=listKey;}if(!runs.some(r=>r.id===current)){if(pendingJob){$("notice").textContent="任务已排队，等待本地进程启动…";return;}current=runs[0]?.id||"";snapshotKey="";}select.value=current;}
    if(!current){currentActive=false;$("notice").textContent="还没有运行记录。填写上方任务，点击开始即可查看目标、结果和问题。";for(const id of ["planner","worker","handoffs","checks","issues","result"])$(id).replaceChildren();$("request").textContent="等待任务记录";$("project").textContent="";$("patch").textContent="";$("task-state").textContent="—";$("issue-count").textContent="0";$("issue-count").className="badge";$("progress").hidden=true;renderFailure(null);document.title="Stage Relay · 协作面板";return;}
    const requested=current,data=await api("api/run/"+requested),key=JSON.stringify({...data,observed_at:0});
    if(requested!==current)return;
    currentActive=["queued","running","ready"].includes(data.state);
    if(force||key!==snapshotKey){render(data);snapshotKey=key;}else $("updated").textContent="本地读取 "+new Date(data.observed_at*1000).toLocaleTimeString()+" · 日志暂无变化";
  }catch(error){$("notice").className="notice error";$("notice").textContent=error.message+"；保留上次内容，稍后自动重试。";}finally{busy=false;}
}
$("run").addEventListener("change",()=>{current=$("run").value;snapshotKey="";for(const role of ["planner","worker"]){delete phaseChoice[role];delete outputScroll[role];}refresh(true);});
$("refresh").addEventListener("click",()=>refresh(true));
function updateHistorySelection(){
  const eligible=historyRows.filter(row=>row.can_delete),ids=new Set(eligible.map(row=>row.id));
  for(const id of selectedRuns)if(!ids.has(id))selectedRuns.delete(id);
  const all=$("run-select-all");all.checked=eligible.length>0&&selectedRuns.size===eligible.length;all.indeterminate=selectedRuns.size>0&&!all.checked;all.disabled=historyBusy||!eligible.length;
  $("run-selected-count").textContent="已选 "+selectedRuns.size+" 条 · 可删除 "+eligible.length+" 条";
  $("delete-selected").disabled=historyBusy||!selectedRuns.size;
  for(const box of $("run-selection-list").querySelectorAll("input")){box.checked=selectedRuns.has(box.dataset.runid);box.disabled=historyBusy||!ids.has(box.dataset.runid);}
}
function renderHistoryChoices(rows){
  historyRows=rows;const host=$("run-selection-list");host.replaceChildren();
  for(const row of rows){const line=node("div",undefined,"history-select-row"),label=node("label"),box=node("input");box.type="checkbox";box.dataset.runid=row.id;box.setAttribute("aria-label","选择任务："+(row.title||row.name));box.addEventListener("change",()=>{if(box.checked)selectedRuns.add(row.id);else selectedRuns.delete(row.id);updateHistorySelection();});
    const text=node("span",undefined,"history-select-text");text.append(node("strong",(row.mode==="mock"?"[模拟] ":"")+(row.title||row.name)),node("small",(row.updated_at?new Date(row.updated_at*1000).toLocaleString():"时间未知")+" · "+(labels[row.status]||row.status)+(row.can_delete?"":" · 运行中，不能删除")));
    label.append(box,text);const view=node("button","查看");view.type="button";view.addEventListener("click",()=>{current=row.id;snapshotKey="";refresh(true);});line.append(label,view);host.append(line);}
  if(!rows.length)host.append(node("p","还没有任务记录。","empty"));updateHistorySelection();
}
$("run-select-all").addEventListener("change",()=>{selectedRuns.clear();if($("run-select-all").checked)for(const row of historyRows)if(row.can_delete)selectedRuns.add(row.id);updateHistorySelection();});
$("delete-selected").addEventListener("click",()=>changeHistory("delete",[...selectedRuns]));
async function changeHistory(action,selection) {
  const ids=selection||(action==="delete"?(current?[current]:[]):lastDeletedIds);if(!ids.length||historyBusy)return;
  historyBusy=true;$("undo-delete").disabled=true;updateHistorySelection();
  try{
    const response=await fetch(new URL("api/history",document.baseURI),{method:"POST",headers:{"Content-Type":"application/json","X-Relay-Panel":"settings"},body:JSON.stringify({ids,action})});
    const result=await response.json();if(!response.ok)throw new Error(result.error||"任务记录操作失败");
    if(action==="delete"){lastDeletedIds=ids;for(const id of ids)selectedRuns.delete(id);if(ids.includes(current))current="";$("history-message").textContent="已从列表删除 "+ids.length+" 条记录，可一键撤销。日志和防重复提交记录仍保留。";}
    else{current=ids[0];lastDeletedIds=[];$("history-message").textContent="已恢复 "+ids.length+" 条记录。";}
    $("undo-delete").hidden=!lastDeletedIds.length;snapshotKey="";runsKey="";listTick=0;
  }catch(error){$("history-message").textContent=error.message;}
  finally{historyBusy=false;$("undo-delete").disabled=false;updateHistorySelection();await refresh(true);}
}
$("undo-delete").addEventListener("click",()=>changeHistory("restore"));
refresh(true);setInterval(refresh,1500);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh(true);});
window.addEventListener("focus",()=>refresh(true));

function callerReady(){try{RelayCompose.callerPlan(callerDraft,$("launch-project").value,$("launch-request").value,$("launch-extra").value);return true;}catch(error){return false;}}
function launchMode(){
  const mode=$("launch-mode").value;
  $("request-label").hidden=mode==="plan";$("request-title").textContent=mode==="caller"&&callerDraft?(callerDraft.invocation_id?"从当前对话带入的任务":"上次保存的任务（可点击新任务切换）"):mode==="caller"?"给当前 Codex 的任务指示":"给规划模型的任务指示";
  $("launch-request").readOnly=mode==="caller"&&!!(callerDraft?.invocation_id||callerDraft?.plan);
  $("caller-copy").hidden=mode==="caller"&&callerReady();
  $("caller-copy").textContent=callerDraft?.status==="blocked"?"复制修正方案请求":callerDraft?.invocation_id?"方案未就绪？复制继续规划请求":"复制任务给 Codex 规划";
  $("plan-label").hidden=mode!=="plan";$("caller-draft").hidden=mode!=="caller";
  const ready=mode!=="caller"||callerReady();
  $("launch-start").disabled=!launchEnabled||!!pendingJob||!ready;
  $("launch-start").textContent=pendingJob?"任务进行中…":!ready?"等待 Codex 方案":repeatReady?"再次运行":"开始任务";
  $("launch-start").title=!ready?"方案尚未就绪，面板会自动读取；当前 Codex 停住时可复制继续规划请求。":"";
  if(mode==="caller")$("caller-note").textContent=callerDraft?.status==="blocked"?"方案准备失败，原因和处理方法见下方。可复制修正请求给当前 Codex。":ready?"方案已就绪。补充要求可选，核对后直接点击下方“开始任务”。":"等待当前 Codex 写入方案，面板会自动读取。正常调用会接着完成规划，无需重复发送任务；若对话已经停住，可复制继续规划请求。";
  $("caller-note").className="hint caller-plan-status "+(callerDraft?.status==="blocked"?"blocked":ready?"ready":"waiting");
  for(const id of ["launch-worker-provider","launch-worker-model","worker-apply"])$(id).disabled=!!pendingJob;
  updateLaunchModels();
}
let launchRoles={};
function updateLaunchModels(){const caller=$("launch-mode").value!=="serial";$("launch-models").textContent=(caller?"规划：当前对话":"规划："+(launchRoles.planner?.model||"未配置"))+" → 执行："+(launchRoles.worker?.model||"未配置")+" · 结果由你确认，或选择 Codex / 已有模型审核";}
async function loadDraft(automatic=false){
  if(draftLoading||pendingJob)return;draftLoading=true;lastDraftCheck=Date.now();
  try{
    const data=await api("api/draft"+(automatic&&loadedDraftId?"?after="+encodeURIComponent(loadedDraftId):""));
    if(data.changed===false)return;
    if(data.draft?.id===dismissedDraftId)return;
    const previous=callerDraft;callerDraft=data.draft;
    const preview=$("draft-preview");preview.replaceChildren();
    if(callerDraft){
      const fields=RelayCompose.syncDraft(previous,callerDraft,$("launch-extra").value);
      $("launch-project").value=fields.project;$("launch-request").value=fields.request;$("launch-extra").value=fields.supplement;
      loadedDraftGoal=fields.request;loadedDraftId=callerDraft.id||"";
      if(automatic)$("launch-mode").value="caller";
      if(!callerDraft.plan){
        preview.append(node("p",callerDraft.status==="blocked"?"这次任务准备失败。":"任务已带入，方案尚未写入。就绪后“开始任务”会自动可用，无需点击刷新。",callerDraft.status==="blocked"?"form-error":"hint"));
        if(callerDraft.error){preview.append(node("p",callerDraft.error.message,"form-error"),node("p","处理方法："+callerDraft.error.action),node("small","错误代码："+callerDraft.error.code));}
        if(callerDraft.generated_project)preview.append(node("p","已为这次独立创作任务建立工作目录："+callerDraft.project,"hint"));
        launchMode();return;
      }
      if(!callerDraft.invocation_id)preview.append(node("p","这里显示的是上次保存的任务，可点击新任务切换。","hint"));
      const sheet=node("section",undefined,"caller-plan-sheet");
      const heading=node("div",undefined,"caller-plan-heading");heading.append(node("span","Codex 方案正文","caller-plan-source"),node("span","待你确认","caller-plan-confirm"));sheet.append(heading);
      sheet.append(node("span","任务目标","caller-plan-label"),node("p",callerDraft.plan.goal,"caller-plan-goal"));
      readable(sheet,{steps:callerDraft.plan.steps},{stepsInDetails:(callerDraft.plan.steps||[]).length>6});preview.append(sheet);
      const location=node("details",undefined,"caller-plan-reference");location.append(node("summary","查看执行目录"),node("p",callerDraft.project,"path"));preview.append(location);
      const facts=node("details",undefined,"caller-plan-reference"),constraints=callerDraft.plan.constraints||[];
      facts.append(node("summary","执行要求与相关文件（"+constraints.length+" 条要求）"));
      readable(facts,{files:callerDraft.plan.files,decisions:callerDraft.plan.decisions,constraints});
      facts.append(detail("完整任务安排（高级）",callerDraft.plan,"caller-draft-json"));preview.append(facts);
    }else preview.append(node("p","还没有可执行的任务安排。请先把任务发给当前 Codex。","hint"));
    launchMode();
  }catch(error){$("draft-preview").textContent=error.message;}
  finally{draftLoading=false;}
}
async function launchOptions(){
  try{const data=await api("api/launch");launchEnabled=data.enabled;launchRoles=data.roles;launchProfiles=data.profiles||[];launchRevision=data.revision;
    if(data.version)$("panel-version").textContent="面板 "+data.version;
    if(data.shortcut){$("shortcut-row").hidden=false;$("shortcut-path").textContent=data.shortcut;}

    const providers=$("launch-worker-provider");providers.replaceChildren();for(const profile of launchProfiles)providers.append(Object.assign(node("option",profile.name),{value:profile.id}));providers.value=launchRoles.worker?.provider_id||"";launchMode();await Promise.all([loadDraft(),loadWorkerModels(),loadChecks()]);
    if(!data.enabled)$("launch-message").textContent="此入口仅供查看；通过 Stage Relay 插件打开面板后可发起任务。";
  }catch(error){$("launch-message").textContent=error.message;}
}
async function localPost(path,body){const response=await fetch(new URL(path,document.baseURI),{method:"POST",headers:{"Content-Type":"application/json","X-Relay-Panel":"settings"},body:JSON.stringify(body)});const value=await response.json();if(!response.ok)throw new Error(value.error||"本地操作失败");return value;}
async function loadWorkerModels(refresh=false){
  const id=$("launch-worker-provider").value,profile=launchProfiles.find(p=>p.id===id),epoch=++workerEpoch,select=$("launch-worker-model");
  const preferred=id===launchRoles.worker?.provider_id?launchRoles.worker?.model:select.value;
  select.replaceChildren(Object.assign(node("option","请选择执行模型"),{value:""}));for(const model of profile?.models||[])select.append(Object.assign(node("option",model),{value:model}));if(preferred)select.value=preferred;
  $("worker-note").textContent="正在读取所选接口的完整模型目录…";$("worker-refresh").disabled=true;
  try{const data=await localPost("api/worker-models",{provider_id:id,refresh});if(epoch!==workerEpoch)return;select.replaceChildren(Object.assign(node("option","请选择执行模型（API 目录）"),{value:""}));for(const model of data.models)select.append(Object.assign(node("option",model),{value:model}));if(data.models.includes(preferred))select.value=preferred;
    $("worker-note").textContent="目录来源："+data.endpoint+" · 接口返回 "+data.models.length+" 个 · 原先保存 "+data.saved_count+" 个。列表来自 API 声明，生成与工具能力未验证。选择后可直接开始任务，或点“应用选择”保存。";
  }catch(error){if(epoch===workerEpoch)$("worker-note").textContent=error.message+" 当前仅显示已保存模型，未确认完整目录及生成能力。";}
  finally{if(epoch===workerEpoch)$("worker-refresh").disabled=false;}
}
async function applyWorker(){
  const provider_id=$("launch-worker-provider").value,model=$("launch-worker-model").value;
  if(!provider_id||!model)throw new Error("请选择执行模型接口和模型。");
  if(provider_id===launchRoles.worker?.provider_id&&model===launchRoles.worker?.model)return;
  const data=await localPost("api/worker",{revision:launchRevision,provider_id,model});launchRoles=data.roles;launchProfiles=data.profiles;launchRevision=data.revision;updateLaunchModels();
  $("worker-note").textContent="已应用执行模型："+model+"。未发起生成请求，生成和工具能力尚未验证。";
}
$("launch-worker-provider").addEventListener("change",()=>loadWorkerModels());
$("launch-worker-model").addEventListener("change",()=>{$("worker-note").textContent="已选择 "+$("launch-worker-model").value+"；开始任务时自动应用，也可点“应用选择”保存。生成能力未验证。";});
$("worker-refresh").addEventListener("click",()=>loadWorkerModels(true));
$("worker-apply").addEventListener("click",async()=>{try{await applyWorker();}catch(error){$("worker-note").textContent=error.message;}});
$("launch-mode").addEventListener("change",launchMode);
$("caller-new").addEventListener("click",()=>{
  dismissedDraftId=callerDraft?.id||loadedDraftId;callerDraft=null;loadedDraftGoal="";
  $("launch-request").value="";$("launch-extra").value="";$("draft-preview").replaceChildren();
  launchMode();$("launch-request").focus();
});
$("launch-project").addEventListener("input",()=>{launchMode();$("launch-message").textContent="项目目录已更改；点击开始将重新检查项目。";});
for(const id of ["launch-request","launch-extra"])$(id).addEventListener("input",launchMode);
$("caller-copy").addEventListener("click",async()=>{
  try{
    const text=RelayCompose.handoff($("launch-project").value,$("launch-request").value,$("launch-extra").value);
    $("caller-copy-preview").hidden=false;$("caller-copy-text").value=text;
    try{await navigator.clipboard.writeText(text);$("caller-note").textContent="规划请求已复制。粘贴到当前 Codex 对话中发送；方案就绪后会自动显示，无需刷新。";}
    catch(error){$("caller-copy-text").select();$("caller-note").textContent="请复制下面这段文字，粘贴到当前 Codex 对话中发送。";}
  }catch(error){$("caller-note").textContent=error.message;}
});
$("launch-form").addEventListener("submit",async event=>{
  event.preventDefault();$("launch-start").disabled=true;
  try{
    const body={project:$("launch-project").value.trim()};
    const extra=$("launch-extra").value.trim();
    body.review_mode="user_choice";
    body.subagents=delegationSelection();
    if($("launch-mode").value==="caller"){body.plan=RelayCompose.callerPlan(callerDraft,body.project,$("launch-request").value,extra);body.user_request=RelayCompose.executionRequest($("launch-request").value,extra);body.draft_id=callerDraft.id;}
    else if($("launch-mode").value==="plan")body.plan=RelayCompose.withSupplement(JSON.parse($("launch-plan").value),extra);
    else body.request=RelayCompose.executionRequest($("launch-request").value,extra);
    await applyWorker();
    const signature=JSON.stringify({body,config_revision:launchRevision});
    if(signature!==launchSignature){launchSignature=signature;launchKey="panel-"+crypto.randomUUID().replaceAll("-","");}
    body.task_key=launchKey;
    const response=await fetch(new URL("api/jobs",document.baseURI),{method:"POST",headers:{"Content-Type":"application/json","X-Relay-Panel":"settings"},body:JSON.stringify(body)});
    const result=await response.json();if(!response.ok){if(result.diagnostic)renderFailure(result.diagnostic);throw new Error(result.error||"提交失败");}
    pendingJob=result.job_id;current=result.run_id;snapshotKey="";
    $("launch-message").textContent="任务已提交；重复点击同一任务会复用记录。";refresh(true);
  }catch(error){$("launch-message").textContent="未提交任务："+error.message;}
  finally{launchMode();}
});
launchOptions();

