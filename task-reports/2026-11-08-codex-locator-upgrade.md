# Codex 内置程序定位修复与 0.3.2 升级包

## 结果

已修复 ChatGPT 桌面应用内置 Codex 的定位逻辑，并构建 Stage Relay 0.3.2 Windows 版。无需另外安装 Codex。旧版和 DSH 仍在运行；为避免中断已派发给 DSH 的续作任务，新版尚未启动。

## 做了什么

- `relay/native_apps.py` 优先从 Windows 注册的 OpenAI 桌面应用包中定位 `app/resources/codex.exe`，不再依赖 Codex 私有 `PATH`。找不到时提示重新检测位置，不再要求安装第二个 Codex。
- `web/app.js` 将“协作工具收到调用”“原生 Agent 在线”“项目工作会话已绑定”显示为不同状态，避免把工具回执误报成原生 Agent 已连接。
- `desktop.py` 支持侧边升级时沿用最近旧版的数据目录；`relay/connections.py` 在桥接程序仍存在且项目、端口、数据目录一致时保留已授权入口。
- Windows 包已构建至 `release/StageRelay-0.3.2-codex-dsh-20261108-210016/Stage Relay/Stage Relay.exe`，并生成便携包 `release/StageRelay-0.3.2-codex-dsh-Windows-x64.zip`。

## 如何确认

- PyInstaller 成功构建桌面应用和 `StageRelayBridge.exe`。
- 压缩包包含 118 个条目，大小约 24.4 MB；检查确认不含 `data/`、桥接凭据或授权记录。
- 当前面板窗口仍运行 0.3.1，DSH 窗口仍打开。当前续作任务在本地记录中为已派发、排队状态。未关闭或重启任一应用，也未声称新版已在实际桌面会话中验证。

## 后续切换

等 DSH 当前续作告一段落后，关闭旧版 Stage Relay 并打开 0.3.2 exe。新版会从同一 `release/` 下自动找到 0.3.1 的数据目录，保留现有任务和桥接授权；本机接入配置使用的旧桥接 exe 仍保留在原目录。启动新版后再检查 Codex 是否在线，以及 DSH 会话是否恢复心跳。
