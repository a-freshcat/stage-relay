# 包容器环境不继承：实测证据（2026-11-10）

## 结论

`Invoke-CommandInDesktopPackage` 启动的子进程使用包身份自身的环境，**不继承调用方设置的任何环境变量**。因此 Stage Relay 无法通过设置 `USERPROFILE`／`HOME`／`CODEX_HOME` 把打包版 Codex 指向隔离实例；它读到的必然是真实用户的 `~\.codex\config.toml`。

这条此前只是推断（由「隔离会话自述规则返回 v2、信箱返回另一个项目」反推），现在有直接实测。

## 实测方法

用与 `relay/windows_codex.py` 完全相同的启动方式，在 Codex 包上下文里运行一个只回读环境的小脚本，结果写入文件后由调用方读出（包容器内 stdout 不回传，因此不能用管道）。

```
包家族名   OpenAI.Codex_2p2nqsd0c76g0
启动命令   Invoke-CommandInDesktopPackage -PackageFamilyName 'OpenAI.Codex_2p2nqsd0c76g0'
           -AppId 'App' -Command '<python>' -Args '"<probe.py>"' -PreventBreakaway
调用方环境 USERPROFILE=<隔离假 home>   RELAY_PROBE=inherited-value
```

## 实测结果

容器内脚本看到的环境：

```json
{
  "USERPROFILE": "C:\\Users\\handsomeboys",
  "HOME": null,
  "CODEX_HOME": null,
  "RELAY_PROBE": null,
  "TEMP": "C:\\Users\\HANDSO~1\\AppData\\Local\\Temp"
}
```

三点判断：

1. `USERPROFILE` 是真实用户目录，**不是**调用方设置的隔离假 home。
2. 我特意设的 `RELAY_PROBE=inherited-value` 在容器内为 `null`，证明环境整体未被继承，而不是只有个别变量被覆盖。
3. `CODEX_HOME`、`HOME` 均为 `null`，说明无法通过它们改道；Codex 只能按包身份去找用户 profile 下的配置。

## 它对验证口径的影响

- 「隔离实例 + 面板派发 + 打包版 Codex」三者无法同时成立：隔离要求改道，打包启动不允许改道。
- 此前在隔离实例上验证到的「`report_request` 生成 job、`delivered`、receipt、generation」这一段仍然成立（那段不依赖 Codex 读哪个配置）。
- 真正需要「面板点按钮 → 该实例会话自主读取并提交」的完整闭环，只能在真实配置（4318）下验证，或改用非打包方式启动 Codex（`codex.exe app-server --stdio`，可继承环境），但后者绕过了面板派发。

## 复现命令要点

```
$env:RELAY_PROBE='inherited-value'
$env:USERPROFILE='<隔离假 home>'
powershell -NoProfile -NonInteractive -Command "Invoke-CommandInDesktopPackage \
  -PackageFamilyName 'OpenAI.Codex_2p2nqsd0c76g0' -AppId 'App' \
  -Command '<python.exe>' -Args '\"<probe.py>\"' -PreventBreakaway"
```

`probe.py` 读 `os.environ` 并写文件；探针目录与假 home 已删除，未改动真实配置与运行实例。