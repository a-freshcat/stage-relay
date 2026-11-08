# Codex 写入冲突与群聊表达补充检查

用户截图显示原生错误 `thread … already has an active writer`，表示同一原生会话的写入拥有权发生冲突。尚未复现或确定具体占用者，不能据此认定用户重复启动窗口。

只读检查发现 relay/codex.py 在 CodexHost 初始化时恢复已保存会话，model-select 也再次调用 thread/resume。两处均列入后续复现范围；启动恢复、连接并发、旧 worker 生命周期与桌面占用也需要区分验证。没有关闭任何真实会话、抢占写入权或修改业务代码。

协作信箱确认当前项目 paused=true。本轮沿用用户的续作提示词准备工作，把连接冲突修复、清楚的错误提示、通俗群聊汇报与技术详情入口加入 handoff/NEXT-CLEANUP-TASK.md，并排在消息/任务清理之前。完整修复和原生回归验证仍待执行，未标记为完成。
