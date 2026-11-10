# 点名唤醒返修复核：源码与隔离回归通过

## 结果

独立复核 DSH 提交 `8e775852a770eb80fbf1f7eddcca0c902484c12b`。原交接与后续消息积压两个探针均通过，相关 89 项 unittest 测试通过。此次结论限于源码及隔离运行时，未验证正在运行的面板已加载该提交，未宣告真实端到端或整个 Antigravity 板块通过。

## 改动与理由

`relay/runtime.py` 新增 `_message_position`，按消息在列表的位置与最近用户消息位置计算预算；议题和项目群聊共用。后续成员消息不改变先前消息的资格，后续用户发言仍可释放挂起事件。点名消息的有限预算仍保留，未取消防无限互发限制。

## 独立执行结果

```text
.venv\Scripts\python.exe qa\mention_bound_probe.py
received_before_user=true; deferred_before_user=[]
PASS (exit=0)
data=qa/mention-bound-e5u0ogcq

.venv\Scripts\python.exe qa\mention_bound_probe.py --backlog
received_before_user=true; deferred_before_user=[[9,"issue-discussion-bound"]]
PASS (exit=0)
data=qa/mention-bound-6gp2lbq2

.venv\Scripts\python.exe -m unittest discover -s tests -p test_context.py -q
Ran 28 tests in 4.481s / OK

.venv\Scripts\python.exe -m unittest discover -s tests -p test_workflows.py -q
Ran 61 tests in 3.508s / OK
```

最初尝试 pytest 返回 `No module named pytest`，随后使用项目测试支持的 unittest 运行；未安装依赖，未将 DSH 的“189 passed”当作本轮实测结果。上述测试经权限批准在项目 qa 隔离数据库运行，未修改真实项目数据。

## 后续

请 DSH 继续准备与提交对应的候选包，并记录面板实际运行的版本/构建来源；尚不能根据本次收到点名就声称运行版缺陷消失。真实成员点名、无用户追问的唤醒与回复仍需验证。

Antigravity 真实应用端到端测试仍未完成。此前已经授权继续非破坏性启动排查；不需要再次等待“是否允许排查”的答复。删除锁或用户数据、关闭共享应用、替用户批准原生权限都不在该授权内。当前“锁占用”尚缺直接证据。

关联事件 #487；项目 project_4d8879b7ef2c；主板块 task_d435b781981a 保持进行中。
