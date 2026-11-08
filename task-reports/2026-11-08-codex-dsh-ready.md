# Codex + DSH 原生任务组交付

## 结果

已完成本轮收敛范围：Codex 与 DSH 可以通过真实原生 Agent 在任务组讨论、执行、独立核验并汇总。交付 Windows 版 `0.3.1-codex-dsh`，并准备了一个尚未派发的本项目续作任务组，供用户交给 DSH 继续开发。

本轮暂缓 Antigravity 与完整自动清理。清理需求和当前边界已经写入 `PRODUCT_REQUIREMENTS.md` 与 `handoff/DSH-CONTINUE.md`。

## 接入方式

- DSH 在当前原桌面应用内加载连接插件，直接使用原生会话、预设、模型、插件、技能和子 Agent。
- Codex 使用已安装程序提供的原生 app-server 后台会话，沿用登录、配置和可用本地插件。该会话独立保存，不是用户当前桌面对话的副本；桌面专属连接器没有逐一验证。
- 面板只负责任务、议题、回执和权限交互，不直接请求裸模型 API，也没有自建另一个代码执行器。原生权限请求由用户决定，不自动批准未知请求。

接口依据为 [OpenAI 官方 app-server 文档](https://learn.chatgpt.com/docs/app-server)。

## 沙箱修复

直接启动 MSIX 包内的 Codex 可复现 `registered Core process has no package identity`，导致原生命令沙箱无法运行。相同命令在保留 Windows 包上下文的启动方式下通过验证。因此通过微软提供的包上下文启动方式配合认证命名管道保持身份，保留原生沙箱，不修改系统安全设置。[Microsoft 官方接口说明](https://learn.microsoft.com/en-us/powershell/module/appx/invoke-commandindesktoppackage?view=windowsserver2025-ps)

用户拒绝过的那次沙箱外只读命令没有执行。修复后，Codex 在原生工作区沙箱内完成了独立检查。设备验证不能由本应用伪造；用户仍可拒绝请求。

## 实际验证

- 真实跨应用任务组：双方回复分工议题 → DSH 写入文件 → Codex 独立检查内容与字节 → Codex 核验通过 → 指定 Codex 汇总。测试中的用户派发和确认由测试脚本模拟，Agent 回复、工具调用与成果文件来自真实执行。
- Codex 接口返回的配置模型为 `gpt-6.1-sol`；DSH 使用用户指定的 `buddy/deepseek-v4.1-flash`。此前还在 DSH 中验证过真实原生子 Agent 委派，以及父、子会话日志中的模型记录。
- 真实跨应用证据：`qa/native-probe-codex-dsh-20261108-195250/result.json`，该目录为本机测试数据，不进入发布包。
- 最终发布 exe 的隐藏 worker 保留真实 Codex 包身份，在原生沙箱内执行标记命令，退出码为 0。
- 最终发布 exe 的 HTTP 检查：12 次缺少授权头的较大 POST 请求均完整返回 403。修复了 Windows 下提前关闭未读请求体导致连接重置的问题，授权限制保留。
- 使用打包的协作桥运行 56 项测试，全部通过，包含真实 Codex 沙箱回归、权限拒绝、跨会话身份、工作流和配置合成检查。
- `web/app.js`、DSH 原生插件语法检查通过；0.3 续作任务组界面已通过独立浏览器截图检查，截图位于 `qa/stage-relay-v0.3.png`。

## 已准备好的续作

本地新版程序的数据中已有“Stage Relay：Codex 与 DSH 续作任务组”。工作区指向当前源项目：

`C:/Users/handsomeboys/Documents/ChatGPT/new_subagent`

Codex 负责方案、监督和汇总，DSH 负责实现。待续板块为“接手续作：完善任务结束与安全清理”，可由用户先修改范围。任务没有派发，也没有提前让 DSH 修改源代码。

旧版记录通过 SQLite 快照复制到新版自己的数据目录，旧版数据未覆盖。发布压缩包排除了本机任务数据库、桥接凭据和其他 `data/` 内容；本地已准备的续作任务保存在解压程序目录的 `data/` 中。

## 怎么试

1. 关闭旧版 Stage Relay 窗口，打开 `release/StageRelay-0.3.1-codex-dsh/Stage Relay/Stage Relay.exe`，保留整个程序文件夹。
2. 在已准备的续作任务组里，分别点击 Codex、DSH 的“接入”，授权并保留“为当前项目自动创建并绑定原生工作会话”的勾选。Codex 兼任汇总，不需要重复接入。
3. 在 DSH 卡片的模型选择器中选择已验证的 `buddy/deepseek-v4.1-flash`。这是会话设置，不修改 DSH 全局默认模型。
4. 确认或编辑待续板块后点击“派发任务”，在议题群聊中查看真实讨论。需要额外原生权限时，面板会展示确认入口。

运行期间保持面板开启，关闭前先暂停任务。回执不确定的工作不会自动重复执行，需先查看原生活动和实际文件。

## 交付位置

- 程序：`release/StageRelay-0.3.1-codex-dsh/Stage Relay/Stage Relay.exe`
- 完整程序压缩包：`release/StageRelay-0.3.1-codex-dsh-Windows-x64.zip`，已检查压缩完整性，不含本地任务与凭据。
- 交接说明：`handoff/DSH-CONTINUE.md`，程序目录也附有 `DSH续作说明.md`。

本机已验证上述链路；其他 Windows 环境及全部第三方插件兼容性仍需要分别验证。
