# 点名后未回应：诊断结果（修复待提交）

## 结果

已确认可复现的派发缺陷：议题中第四条成员消息已保存且点名正确，但被运行时“四条消息上限”挂起，用户再次发言才释放。已向 DSH 回复 Antigravity 实现方案，同时交接这项修复。当前未修改业务源码，未声称修复完成。

## 真实现场

项目 `project_4d8879b7ef2c`，议题 `issue_6e4c128d6ba1`。事件 #472 为 DSH 07:10:52 UTC 发给 Codex 的方案，事件 #474 为用户 07:27:10 UTC 追问，间隔 16 分 18 秒。

只读查询真实 4318 服务：运行版 0.1.4。任务表结果：

```text
delivery_4dc887c1c121 event=472 status=delivered updated=1794295631.280755 merged_events=[474]
delivery_315471a6ac4f event=474 status=coalesced updated=1794295631.230117
native_deferred for codex / events 472,474: []
```

这些记录证明消息合并后送达、时间晚于追问；当前 deferred 为空，不能据此直接证明历史入表。本轮另用隔离复现确定相同消息顺序触发限额。

## 可重复验证

新增 `qa/mention_bound_probe.py`，只在 qa 新建独立测试数据库，使用真实 Store / Runtime 调用。三条 Codex 交接后，DSH 第四条点名 Codex，随后由用户发言。运行命令：

```text
.venv\Scripts\python.exe qa\mention_bound_probe.py
{"received_before_user": false, "deferred_before_user": [[5, "issue-discussion-bound"]], "received_after_user": true}
FAIL: explicitly addressed handoff waits for a further user message
exit code: 1
```

完整输出还含隔离数据目录 `qa/mention-bound-he0_4mua`。失败是预期的缺陷信号。沙箱内首次运行因 SQLite 无法创建数据库失败；权限批准后运行得到上面业务复现结果。未修改真实项目记录。

源码 `relay/runtime.py:543` 起对整个当前议题计数，`len(issue['messages'])-latest_user-1>=4` 时将消息放进 native_deferred，尚未区分消息本身已经获准写入和后续自动讨论预算。项目群聊也有同类判断。对整个当前历史计数还可能让后续消息压住更早未派发事件。

## 后续

DSH 接到修复交接：确保第四条已允许消息可送达，按对应事件计算资格；保留防循环控制，不让任意点名无限绕过上限，检查议题与项目群聊，清楚显示等待原因。提交修复后由 Codex 独立复跑此探针和相关回归。目前尚无修复或部署结果。

Antigravity 主线继续：允许原生接口适配与已授权真实应用测试；独立 language_server 探针不等同于完整应用端到端。需证明应用中的同一会话、原生工具与 MCP、权限和结果回传。未将接口存在的 400 参数校验误判为一次真实执行成功。
