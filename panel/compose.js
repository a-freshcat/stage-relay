"use strict";
(() => {
  const clean = text => String(text || "").replaceAll("\r\n", "\n").trim();
  const projectKey = text => clean(text).replaceAll("\\", "/").replace(/\/$/, "").toLowerCase();
  function withSupplement(plan, supplement) {
    const result = JSON.parse(JSON.stringify(plan));
    const extra = clean(supplement);
    if (extra && !(result.constraints || []).includes(extra)) result.constraints = [...(result.constraints || []), extra];
    return result;
  }
  function callerPlan(draft, project, request, supplement) {
    if (!draft) throw new Error("请先将任务交给当前 Codex 准备方案，再点击读取当前对话方案。");
    if (projectKey(project) !== projectKey(draft.project)) throw new Error("项目已更改，请交给当前 Codex 为这个项目重新准备方案。");
    if (clean(request) !== clean(draft.user_request || draft.plan.goal)) throw new Error("任务指示已更改，请复制给当前 Codex 重新准备方案；不会使用旧方案执行新任务。");
    return withSupplement(draft.plan, supplement);
  }
  function syncDraft(previous, draft, supplement) {
    const request = draft.user_request || draft.plan.goal;
    const sameTask = previous && projectKey(previous.project) === projectKey(draft.project)
      && clean(previous.user_request || previous.plan.goal) === clean(request);
    return {project: draft.project, request, supplement: sameTask ? String(supplement || "") : ""};
  }
  function executionRequest(request, supplement) {
    return clean(request) + (clean(supplement) ? "\n\n用户在确认时补充的要求（与原方案细节冲突时以本次补充为准）：\n" + clean(supplement) : "");
  }
  function handoff(project, request, supplement) {
    if (!clean(project) || !clean(request)) throw new Error("请先填写项目目录和给 Codex 的任务指示。");
    return "请复用当前对话为以下任务准备 Stage Relay 方案，并使用 relay_prepare 保存到面板供我审阅，暂不执行。\n"
      + "项目：" + clean(project) + "\n任务指示：\n" + clean(request)
      + (clean(supplement) ? "\n补充要求：\n" + clean(supplement) : "");
  }
  globalThis.RelayCompose = {clean, callerPlan, withSupplement, syncDraft, executionRequest, handoff};
  if (typeof module !== "undefined") module.exports = globalThis.RelayCompose;
})();
