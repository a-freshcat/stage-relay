# Stage Relay 续作交接

## 0.3.3 交付状态（2026-11-08，DSH 执行，待 Codex 核验）

项目级群聊与任务结束/资源清理已实现，发布目录为 `release/StageRelay-0.3.3-codex-dsh`（未覆盖运行中的旧版）。自动化检查：78 项测试通过、1 项跳过；`node --check` 与 `qa/ui_probe.mjs` 通过。交付说明见 `task-reports/2026-11-08-project-chat-and-close.md`。

唯一未实测项：当前安装的 DSH 版本是否向插件暴露 `workspaceRegistry.archiveSession`。未暴露时插件回执失败，资源停在「待重试」，不会当作已归档。DSH 插件接口不返回会话父子关系，因此只登记面板创建的会话，不推测原生子会话。

## 用户当前决定

先把 Codex 与 DSH 用起来，再让 DSH 在真实任务组里继续开发本项目。Codex 负责方案、监督和最终核验，DSH 负责实现。Antigravity 暂不接入；自动清理不在本次 Codex/DSH 接入交付内，作为下一板块。

这里要求原生 Agent 调用所属应用的工具、插件、技能和适用的子 Agent，不能以裸模型 API 或演示回复替代实际执行。工作区就是本文件所在项目的根目录。

## 已经实际跑通

- DSH：在正在运行的原应用内加载 Cordis 原生连接插件，创建或绑定工作会话，用原生 followup / steer 接收任务，原生工具和子 Agent 实际执行。
- Codex：复用本机 Codex 原生 app-server，在后台建立独立会话，沿用登录、配置和可用本地插件。它不是当前用户桌面对话的副本；桌面专属连接器不能默认视为兼容。
- Windows 的 MSIX Codex 必须在包上下文启动。直接 Popen 包内 codex.exe 会导致 `registered Core process has no package identity`；当前通过微软 Invoke-CommandInDesktopPackage 与认证命名管道保持身份。不能通过关闭沙箱来替代这个修复。
- Codex 与 DSH 两个真实会话已完成：回复议题 → DSH 写文件 → Codex 在原生沙箱内独立读回核验 → Codex 提交汇总。
- DSH 父、子原生会话均验证过 buddy/deepseek-v4.1-flash；Codex 后台会话返回的配置模型为 gpt-6.1-sol。DSH 项目会话模型可在面板选择，保留原应用全局默认设置。
- 原生权限请求在面板交给用户决定，不自动批准未知权限。用户拒绝过一次沙箱外只读检查；该命令没有执行，随后修复沙箱并在沙箱内完成验证。

## 主要代码

- relay/domain.py：项目、分工、板块、议题、核验与交付，SQLite 事务。
- relay/runtime.py：原生会话绑定、持久化任务派发/回执、同模型通道排队。
- relay/codex.py：Codex 原生协议、任务与事件、用户权限回答。
- relay/windows_codex.py、relay/codex_worker.py：Windows 包身份启动与命名管道传输。
- integrations/dsh/index.mjs：DSH 原进程中的桥，直接调用其原生服务。
- relay/connections.py：检测、授权、备份、配置写入/撤销。
- web/app.js、web/styles.css：面板；desktop.py：WebView2 exe 窗口与工作区选择。
- PRODUCT_REQUIREMENTS.md：已确认需求及当前边界。

## 下一板块：任务结束与后台资源清理

先读取 PRODUCT_REQUIREMENTS.md 的清理要求。先提出可审查的实现方案，并通过 relay_raise_issue 与 Codex 讨论，再完成安全的第一版。

要求：区分“汇总已提交”和“用户结束项目”；记录资源归属；仅归档本项目由面板创建的原生会话及其子会话；手动绑定的已有会话只解绑；保留项目对话、用户决定、检查依据和交付文件；共享应用与插件保留。临时文件清理先做清单和可恢复操作，不执行任意模型生成的清盘脚本，不扫描硬盘猜垃圾，不按进程名批量杀应用。

DSH 原应用提供 workspaceRegistry.archiveSession(sessionId, {stopActivity:true})。此前只阅读确认过接口，应用尚未实现项目结束触发与回收流程。应通过原应用归档，不直接删除原应用数据库。需要实际删除或扩大权限时，先向用户提出明确请求。

先在独立测试目录和测试会话验证；不得清理用户现有任务，也不得为了验证清理功能删除本项目成果。Windows 递归删除或移动前必须核对解析后的绝对路径、资源归属、其他项目引用及是否仍在运行。

## 协作规则

只使用任务消息指定的、属于自己成员身份的 stage-relay MCP 服务。先 relay_inbox，保留游标；进度用 relay_report，遇到困难或方案变更用 relay_raise_issue，组内讨论用 relay_send_message。paused=true 时停止推进。执行成果先提交 review，DSH 不自验；Codex 读取实际文件和检查结果后用 relay_review 核验。用户保留最后决策权。

不要把未知状态写成已完成。送达回执、模型返回和通过验收是三个不同阶段。

## 验证与打包

常规测试：`python -m unittest discover -s tests -q`；前端检查：`node --check web/app.js`。显式开启真实 Codex 沙箱测试时设置环境变量 RELAY_TEST_NATIVE_CODEX=1。`qa/codex_sandbox_probe.py` 是快速原生沙箱检查；它不能用沙箱外执行作为“通过”。

`qa/collaboration_probe.py --codex` 会临时修改真实应用接入，虽然有备份和自动撤销，也不要在其他会话正在修改接入配置时并行运行。

构建：`.venv/Scripts/python.exe build_desktop.py`。输出到新的 release 目录，保留完整目录；不能覆盖正在运行的 exe。data/、桥接凭据和 qa 测试状态不进入发布压缩包。

浏览器视觉检查辅助交付，失败不能单独阻断交付。需要 headless 时使用受支持模式、独立临时配置目录、绝对 URL、限定超时并保存日志；不得使用 headless=old、single-process、no-zygote，也不得为测试关闭系统安全机制。
