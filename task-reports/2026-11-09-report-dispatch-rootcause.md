# 跟进汇报：面板派发闭环的根因与证据（2026-11-09）

## 结论

1. **修掉一个真实缺陷**：`report_request` 不在 `relay/runtime.py` 的 poll 唤醒白名单里，点按钮后事件被游标跳过、永不生成 job。已补入白名单并加测试锁定，提交 `62f4ef0`。
2. **面板派发本身通了**：隔离桌面实例上，点按钮产生了 `report_request` 类型的 job，状态 `delivered`，回执为 `Accepted by the installed Codex native harness`，prompt 内含项目 ID 与请求号，generation=1。
3. **原生自主提交仍未闭环**，根因已定位到打包启动方式，不是业务代码：`relay/windows_codex.py` 用 `Invoke-CommandInDesktopPackage` 启动 Codex，子进程在 MSIX 包容器内运行，**不继承调用方的 `USERPROFILE`/`HOME`**，因此读到真实用户的 `~/.codex/config.toml`（指向 4318、规则 v2），而不是隔离实例的配置（4443、规则 v3）。

## 二、逐项证据

### job / session / request 关联（隔离实例 4443，数据库 `workspace.sqlite3`）

```
create_619135d8ca6c   codex  event=None  delivered  kind=create-session  gen=1
delivery_79cfed5f8c58 codex  event=4     delivered  kind=assign          gen=1
delivery_1514bfde84e2 codex  event=6     delivered  kind=report_request  gen=1
    receipt: Accepted by the installed Codex native harness
    prompt 含 project_21835e615d7e: True
    prompt 含 dispatch-e2e:        True
```

绑定与资源：

```
native_bindings  project_21835e615d7e  codex  01a1c1f2-93e9-7a13-91f7-9410f7236680
native_resources resource_b9952dbec912  panel-created
codex_threads    01a1c1f2-93e9-…       workspace=qa/native-dispatch/workspace
```

记录状态（未闭环的直接证据）：

```
request_id=dispatch-e2e  status=queued  actor=null  covered_to=5  submitted=null  body=""
```

### 原生两轮转录（`native_activity`，脱敏摘要）

```
turn 1  relay_inbox → relay_rules → relay_project → relay_raise_issue
        自述：规则返回 v2（本次通知要求 v3）；信箱返回另一个项目
turn 2  relay_inbox → relay_project → relay_project_message
        自述：MCP 仍连到另一个项目，目标项目读取失败
        正文写入群聊，未调用 relay_follow_up_report
```

工具结果原始状态：

```
seq 22  relay_inbox        status=completed  isError=false  output=""
seq 24  relay_project      status=failed     isError=true   output=""
seq 26  relay_project_message status=failed  isError=true   output=""
```

### 同一时刻桥的独立调用（对照组）

用同一个打包桥、同一个 data 目录、同一端口直接调用：

```
relay_rules   -> stage-relay/rules/v3
relay_project -> project_21835e615d7e 正常返回
relay_inbox   -> projects 只有 project_21835e615d7e
```

即：桥本身指向正确实例；只有包容器内那个会话指向别处。两者对照排除了桥、数据目录、面板状态。

### 根因定位

```
relay/windows_codex.py: PackagedCodexProcess.__init__
  script = Invoke-CommandInDesktopPackage -PackageFamilyName … -Command … -Args … -PreventBreakaway
```

`Invoke-CommandInDesktopPackage` 在 MSIX 包容器里启动子进程，环境来自包身份而不是调用方 shell，所以 `USERPROFILE` 重定向不生效。这解释了为什么隔离配置写对了、桥独立调用对了，而会话内工具指向真实 4318。

真实用户 profile 下的配置（只读核对，未修改）：

```
command = …\release\StageRelay-0.1.2-20261110-002858\…\StageRelayBridge.exe
args    = ["--agent","codex","--port","4318","--data-dir",…]
```

这正是 rules v2 与「另一个项目」的来源。

## 三、这意味着什么

- 「点按钮即生成」这件事，**面板侧已证明能派发到原生 harness**。
- 剩下的缺口不是面板派发，而是**隔离验证方法本身不可行**：打包版的 Codex 启动路径带着包身份，无法用环境变量把它指向隔离实例。要真实验证这一环，只能在真实配置下（4318 + 真实会话）点击，或改用非打包方式（`codex.exe app-server --stdio` 直接启动，可继承环境）——后者我上一轮已经做过，但它绕过了面板派发，因此不能同时满足「面板派发」与「隔离」两个条件。

## 四、检查记录

```
python -m pytest tests -q   -> 184 passed, 1 skipped
node qa/ui_probe.mjs        -> ALL UI CHECKS PASSED
新增测试 test_the_panel_button_alone_wakes_the_reporter（锁定白名单）
```

## 五、未闭环

1. 原生在面板派发后自主提交并持久化：未验证。原因是上述包容器环境隔离，不是功能缺陷。
2. 离线悬挂与失败重试：只验证了失败态写入与重试路径，未伪造真实断线。

## 六、保留的证据

`qa/native-dispatch-evidence/`（已脱敏、已入库）：

- `evidence.json`：jobs（id/status/kind/generation/receipt/prompt 是否含项目与请求号）、原生 activity 全文摘要、reports 记录、bindings、codex_threads
- `isolated-config.toml`：隔离实例写入的桥配置（`--port 4443`），与包容器内实际读到的真实配置对照

原始 WebView 运行时缓存与完整 sqlite 已删除；上面这些足够独立复核派发与未提交两件事。