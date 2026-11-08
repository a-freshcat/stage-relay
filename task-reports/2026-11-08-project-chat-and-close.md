# 项目群聊与任务结束/资源清理交付（0.3.3）

## 结果

本轮把两件事做完并交付为 `release/StageRelay-0.3.3-codex-dsh`（新目录，未覆盖运行中的 0.3.1/0.3.2）：

1. 项目级群聊：真实任务没有议题时也有可打开的会话，刷新不丢，成员发言进入其他成员信箱。
2. 用户结束项目：停止派发、撤销未完成板块、取消未送达任务，按资源登记归档面板创建的会话、只解绑手动绑定的会话，并提供固定规则清理预览与可恢复移动。

运行中的旧版发布包、旧版本数据和其他项目未被改动。

## 改动文件

- `relay/domain.py`：`project.chat` 与新 action `project_message`；`project_close`（结束时间、保留期、未完成板块置 `cancelled`、暂停派发）；结束后的写操作统一拒绝；`export_markdown` 增加任务状态与「项目群聊」段落。
- `relay/runtime.py`：新增 `native_resources` 登记表（origin = panel-created / user-bound）；`bind()` 登记手动绑定，`created()` 登记面板创建；`close_project()` 取消未送达任务、删除本项目绑定、只对 panel-created 下发 `archive-session` 指令；`reconcile()` 在启动时处理无回执的归档与过期租约；归档回执更新资源状态；`poll()` 允许归档指令在暂停/结束后送达。
- `relay/cleanup.py`（新）：固定规则预览（本项目 `tmp/<project_id>`、无引用且超保留期的上传附件）与执行（移动到 `recovery/<时间戳>-<项目>/` 并写 `manifest.json`，路径必须在数据目录内，受保护文件拒绝移动）。
- `relay/httpd.py`：`/api/project/close`、`/api/cleanup/preview`（GET/POST）、`/api/cleanup/execute`（必须 `confirm: true`）；`project_message`、`project_close` 加入动作白名单。
- `relay/mcp.py`：新增 `relay_project_message` 工具；其余工具语义不变。
- `integrations/dsh/index.mjs`：处理 `archive-session` 指令，调用原应用 `workspaceRegistry.archiveSession(sessionId,{stopActivity:true})`；能力缺失时回执失败，不伪造成已归档。
- `web/app.js`、`web/styles.css`：统一线程抽屉（议题/项目）、三处入口、`ui.chat/chatMode/issue` 持久化、结束任务二次确认弹窗（含保留期）、结束态侧栏（登记资源状态）、清理预览弹窗与可恢复移动按钮、结束态隐藏派发与新增入口。
- 文档：`README.md`、`PRODUCT_REQUIREMENTS.md` 更新为 0.3.3 的实测状态与已知边界。

## 实测检查

- `.venv\Scripts\python.exe -m unittest discover -s tests -q` → **78 passed, 1 skipped**（本轮新增 20 项：群聊 9 项、结束/资源 3 项、清理 5 项、接口 3 项）。
- `node --check web/app.js`、`node --check integrations/dsh/index.mjs` → 通过。
- `node qa/ui_probe.mjs` → 12 项渲染断言全部通过（结束态侧栏、结束态隐藏派发/新增入口、清理预览候选与保留项、结束确认弹窗）。
- 隔离实例 `python -m relay.httpd --port 4399 --data-dir qa/chat-ui` + headless Edge（`--headless=new`、临时 profile）截图：`qa/chat-ui/overview.png`、`qa/chat-ui/overview-tall.png`、`qa/chat-ui/project-chat.png`。同一 profile 二次加载后抽屉仍打开。
- 打包：`build_desktop.py` 输出 `release/StageRelay-0.3.3-codex-dsh/Stage Relay/Stage Relay.exe`，包内 `_internal/web/app.js` 为新版本（69,952 字节，含 `project_message`/`closedRail`/`cleanupExecute`）。
- 打包版实测：`qa/release_033_check.py` 以独立数据目录启动真实 exe（端口 4345，隐藏窗口），`/api/health` 返回 `0.3.3-codex-dsh`；随后经 HTTP 建任务 → 项目群聊发言 → `/api/project/close` → `/api/cleanup/preview` → `/api/cleanup/execute` 全部 200。输出：`{"release_version":"0.3.3-codex-dsh","packaged_exe_checked":true,"project_closed":true,"chat_messages":1,"planned_archive_or_unbind":0,"preview_items":0,"preview_retained":3}`。该实例没有绑定原生会话，所以归档/解绑计数为 0，属预期。

## 未验证与边界

- 当前安装的 DSH 版本是否向插件暴露 `workspaceRegistry.archiveSession` 未实测。未暴露时回执失败，资源停在「待重试」，不会当作已归档。
- DSH 插件接口不提供会话父子关系，因此只登记面板实际创建的会话，不推测原生子会话。
- Antigravity 不参与结束流程。
- 清理只覆盖两类固定对象；工作区、上传引用、数据库、凭据、接入备份、恢复区一律列为保留项并展示大小。

## 回滚

旧版发布目录与数据保留在原处；删除 `release/StageRelay-0.3.3-codex-dsh` 即可回到旧版。清理执行只移动到 `data/recovery/`，可从该目录按 `manifest.json` 原样移回。
