# 点名唤醒修复独立核验：部分通过，待返修

## 结果

收到 DSH 事件 #480 的提交 `7e0faf4cab48e898d799c84866cbbf95b1d68907`。原四条消息场景已通过，但后续消息积压仍使更早交接被挂起，不能认定完整修复。已向 DSH 交回返修，未修改业务实现、未部署或发布。

## 源码与验证

提交将 Runtime 中点名消息的阈值从 4 提高至 8；仍对整个当前议题/群聊历史计数，没有按对应事件的时间位置判断资格。

仅修改诊断脚本 `qa/mention_bound_probe.py`，增加 `--backlog`：第四条交接后、首次轮询前，追加四条消息，验证后续历史不会撤销较早消息的派发资格。

```text
.venv\Scripts\python.exe qa\mention_bound_probe.py
received_before_user=true; deferred_before_user=[]
PASS; exit=0
隔离数据：qa/mention-bound-qy7yh9mp

.venv\Scripts\python.exe qa\mention_bound_probe.py --backlog
received_before_user=false
deferred_before_user=[[5,"issue-discussion-bound"],[6,"issue-discussion-bound"],[7,"issue-discussion-bound"],[8,"issue-discussion-bound"],[9,"issue-discussion-bound"]]
received_after_user=true
FAIL; exit=1
隔离数据：qa/mention-bound-5azv11n0
```

沙箱内最初运行因 SQLite 无法建测试文件失败；权限批准后两个隔离运行得到上面的业务结果。未重跑 DSH 所称的全套测试，未声称已运行 187 项测试。此次唤醒同时含 DSH 事件 #480 与用户追问 #481；收到合并通知不证明修复已部署或独立唤醒成功。

## 交接

要求 DSH 按消息对应事件计算资格，补议题和项目群聊的积压回归，保留有限自动讨论预算。仍未完成 Antigravity 真实应用端到端验证。DSH 报告启动即退出、日志无更新，但单靠这些不足以证明单实例锁占用；允许继续非破坏性排查，未授权删除锁或用户数据、关闭共享应用或绕过权限。
