# 更正：Antigravity 启动器没有损坏，是我自己的环境造成的假象

## 我错在哪

上一轮我在 `task-reports/2026-11-10-antigravity-launcher-finding.json` 里写下「`Antigravity.exe` 是被改名的 Node 运行时，不是 Electron」，并据此判断「启动器已损坏，需要用户重装」。**这个结论是错的。**

依据只有这几条，而它们全部由我自己的 shell 环境造成：

```
Antigravity.exe --version      -> v24.21.0
Antigravity.exe --help         -> "Usage: node [options] [script.js] [arguments]"
Antigravity.exe -e "..."       -> process.version v24.21.0
--enable-logging               -> "bad option: --enable-logging"
无参数启动                       -> 静默退出 EXIT=0
```

真正原因是我的会话里一直存在：

```
ELECTRON_RUN_AS_NODE = 1
```

Electron 的二进制内嵌了完整的 Node，这个变量会让它以纯 Node 模式运行，于是所有表现都像「被改名的 node」。Codex 用文件元数据（`ProductName=Antigravity`、`ProductVersion=2.29.0.0`）指出矛盾，这个反驳成立。

## 更正后的实测

清掉该变量后：

```
Antigravity.exe --version
  -> DevTools listening on ws://127.0.0.1:54541/devtools/browser/…

headless 启动（ELECTRON_OZONE_PLATFORM_HINT=headless）
  -> 进程：Antigravity ×4 + language_server ×1
  -> main.log 实时更新到 2026-11-10 18:59（此前一直停在 11-08）
  -> language_server.log: listening on 52211 (HTTPS/gRPC), 52212 (HTTP)
```

即：**真实 Antigravity 完全可以启动**，之前「启动即退出、日志零更新、拿不到单实例锁」的全部判断都不成立，单实例锁那段代码压根不是原因。

## 真实应用的语言服务器实测

用真实实例自己的 CSRF token 调用：

```
GET  http://127.0.0.1:52212/            -> __APP_CONFIG__ appVersion 2.30.0, csrfToken 36 字符
POST Heartbeat                          -> 200 {"lastExtensionHeartbeat":"2026-11-10T11:01:04.210Z"}
POST GetAllCascadeTrajectories          -> 200 {}
```

这是此前所有验证都做不到的一步：**跑在真实应用进程里、带真实登录态与真实工作区的语言服务器，可以被外部程序调用。**

## 影响

- 「需要用户重装 Antigravity」这条建议撤回，安装是好的。
- 上一轮那份 launcher-finding 证据作废，不作为任何结论依据。
- 真实应用端到端现在**具备条件**继续做，之前判断「无法进行」不成立。

## 教训

我用自己的 shell 里一个遗留的 `ELECTRON_RUN_AS_NODE=1` 去探测 Electron 应用，却把探测结果当成被测对象的属性。这类环境变量必须在探测前显式清除，否则任何 Electron 程序都会被误判。

## 当前状态

真实 Antigravity 正在运行（headless，4 个进程 + language_server）。我保持在观察状态，不自动批准任何权限弹窗，也不改动用户会话。