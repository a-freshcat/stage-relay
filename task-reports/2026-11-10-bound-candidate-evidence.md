# 点名唤醒：固定候选包与隔离实证

## 提交与构建标识

```
源码快照   8e77585  "the allowance follows the message, so later traffic cannot defer an earlier handoff"
候选包     release/StageRelay-0.1.4-bound/Stage Relay/Stage Relay.exe
包内标识   /api/health -> {"app":"stage-relay","version":"0.1.4-bound"}
```

未覆盖运行包、未发布、未关闭共享应用。

## 隔离实例

```
端口   4451
数据   qa/bound-live/data
HOME   重定向到 qa/bound-live/home（桥配置写入的是隔离 home）
```

## 场景构造（关键：身份必须分开）

第一次构造时我把 `actor` 当参数塞进 `args`，结果十条消息全部以 `user` 身份落库，讨论限额根本没被触发——那次「deferred 为空」什么也证明不了。重做时改走桥的 `/bridge` 通道，用两个真实成员身份分别写入：

```
codex     进度第 0/1/2 条            （members 侧，累计 3 条）
deepseek  @codex 请核验这一项         （event 10，被点名方是 codex）
codex     补充 0/1/2/3               （积压 4 条，全部在点名交接之后）
```

**全程没有任何用户发言。** 落库后核对：

```
messages: codex×3 → deepseek(@codex) → codex×4
event 10  actor=deepseek  recipients=['codex']
```

## 结果

用真实 `Store`+`Runtime` 在隔离数据上跑一次 poll（绑定 `probe-thread-codex`）：

```
jobs leased: 1
  delivery_9481ae181995  kind=assign  merged=[10]
deferred now: []
```

`merged=[10]` 就是那条 `@codex` 点名交接——它在积压 4 条之后、无用户追问的情况下，随派发载荷一起送达。

同一场景在修复前（`7e0faf4`）的形态是：

```
deferred = [[5,bound],[6,bound],[7,bound],[8,bound],[9,bound]]
received_before_user = false
```

## 为什么没有用包容器内会话做「原生回应」

按 Codex 要求不能用 HTTP receipt 代替原生回应，我试了真实原生路径，结果复现了上一轮已实测的根因：

```
原生会话 turn 记录：
  relay_inbox → 描述「收件箱返回另一项目」
  relay_event ×9 → 全部「找不到任务」
  relay_issue     → 同样失败
```

原因不是本次改动：打包版 Codex 经 `Invoke-CommandInDesktopPackage` 在 MSIX 包容器内启动，**不继承调用方环境**（`task-reports/2026-11-10-package-context-environment.md` 有实测：`USERPROFILE` 为真实用户目录、我方设置的探针变量为 null），因此它读的是真实 `~/.codex/config.toml`（4318），看不到隔离实例。

所以「隔离实例 + 面板派发 + 打包版 Codex 的原生回应」三者仍无法同时成立。本次能给出的是：**派发层已把点名交接正确送达**（有 job、merged_events、无 deferred）；原生回应这一环在隔离环境下被包容器挡住。

## 检查记录

```
qa/mention_bound_probe.py            PASS
qa/mention_bound_probe.py --backlog  PASS（积压中仅 event9 超额挂起）
python -m pytest tests -q            189 passed, 1 skipped
node qa/ui_probe.mjs                 ALL UI CHECKS PASSED
```

新增回归：`test_later_member_traffic_cannot_defer_an_earlier_handoff`、`test_a_user_reply_still_releases_a_deferred_handoff`。

## 清理

隔离实例已停止，`qa/bound-live` 保留数据供复核；候选包 `release/StageRelay-0.1.4-bound` 保留。