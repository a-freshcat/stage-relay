# 让当前面板加载 40c6129：步骤与回滚

这份说明里没有一个动作是我已经做的。真实面板仍在跑旧代码，重启需要用户许可。

## 一、当前事实（只读实测）

```
运行进程      pid 26284  `python app.py --open`
启动时间      2026-11-10 17:49:49
GET /api/health
  app         stage-relay
  version     0.1.4
  pid         26284
  data_dir    ...\release\StageRelay-0.3.1-codex-dsh\Stage Relay\data
  antigravity 键不存在        ← 这一版没有 loop 状态，即旧代码
```

代码仓库：

```
HEAD        40c61295d35faba6afc6951185700901d4e87e0b
commit      40c6129
tests       232 passed, 1 skipped
ui_probe    ALL UI CHECKS PASSED
```

磁盘上的代码已经是新的；差别只在进程内存。Python 不会热重载已导入的模块，所以界面看不到 `antigravity` 段与自动派发。

## 二、重启前应先确认的三件事

1. **没有正在执行的派发**。检查 `native_activity` 最新时间与 job 状态：

```powershell
python -c "import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:4318/api/health')),ensure_ascii=False,indent=1))"
```

若 `antigravity` 段为 `null` 或不存在，就没有 loop 在跑（当前正是这种情况）。

2. **备份数据目录**。store 是单文件 SQLite，重启不写它，但备份可让回滚完全无损：

```powershell
$data = "C:\Users\handsomeboys\Documents\ChatGPT\new_subagent\release\StageRelay-0.3.1-codex-dsh\Stage Relay\data"
Copy-Item "$data\workspace.sqlite3" "$data\workspace.sqlite3.before-restart-40c6129.bak"
Copy-Item "$data\connection-ownership.json" "$data\connection-ownership.json.before-restart-40c6129.bak"
```

3. **成员接入配置不动**。`~/.gemini/config/mcp_config.json` 已在接管时改好并验证，重启面板不需要再改它。

## 三、重启步骤（需用户执行或明确授权）

```powershell
# 1) 结束旧面板（只这一个进程）
Stop-Process -Id 26284 -Force

# 2) 在同一目录、用同一命令重新启动
cd C:\Users\handsomeboys\Documents\ChatGPT\new_subagent
python app.py --open
```

预期：`bridge.py --agent deepseek/codex --port 4318` 两个子进程会随新面板重建。`antigravity` 的桥由 Antigravity 自己启动，不属于面板子进程。

## 四、如何确认已加载新代码

```powershell
python -c "import json,urllib.request;h=json.load(urllib.request.urlopen('http://127.0.0.1:4318/api/health'));print(h['pid'], h.get('antigravity'))"
```

判定标准，全部成立才算加载成功：

```
pid 变了（不再是 26284）
antigravity 段存在，形如：
  {"online": true, "cascades": 1, "delivered": 0, "timeouts": 0,
   "last_error": null, "reason": "已连接原生应用", "busy": [], "sessions": {}}
```

若 `online: false`，先看 `reason`：

- `未发现运行中的 Antigravity 语言服务器` → 应用没开，或 LS 进程不在 `--app_data_dir antigravity` 下；
- `原生应用未响应：…` → 端口探测失败，检查 `antigravity_ports()` 是否取到该 pid 持有的端口。

## 五、回滚

### 情形 A：面板起不来或行为异常

```powershell
Stop-Process -Id <新 pid> -Force
cd C:\Users\handsomeboys\Documents\ChatGPT\new_subagent
git stash list                       # 确认没有把别人的改动 stash 掉
git checkout -- relay/               # 只回退 relay 包
python app.py --open
```

注意：工作树里有**不属于我的**未提交改动（`qa/*.py`、`README.md`、`.gitignore`）。`git checkout -- relay/` 只动 `relay/`，不要用 `git checkout .`。

### 情形 B：数据可疑

```powershell
$data = "...\Stage Relay\data"
Stop-Process -Id <新 pid> -Force
Copy-Item "$data\workspace.sqlite3.before-restart-40c6129.bak" "$data\workspace.sqlite3" -Force
Copy-Item "$data\connection-ownership.json.before-restart-40c6129.bak" "$data\connection-ownership.json" -Force
python app.py --open
```

### 情形 C：只想回到已发布的 0.1.4 行为

```powershell
git checkout 40c6129~1 -- relay/
```

仅回退代码，不动数据、不动应用侧 MCP 配置。

## 六、不会受影响的

- `~/.gemini/config/mcp_config.json`：接管结果，重启面板不改它；
- `~/.codex/config.toml`、`~/.dsh/profiles/desktop/cordis.patch.yml`：由 `bridge_refresh` 在每次启动时按既有授权重新指向，已有 `.bak`；
- Antigravity 应用本身与其 cascade：面板不创建也不归档它们；
- 其他项目与 QA 记录。

## 七、已知遗留（不因重启而改变）

- 新建 cascade 首轮仍报 `400 GenerateContentRequest.contents is not specified`，`create_session` 保持拒绝；
- 重启后能看到 loop 状态与自动派发，但绑定既有会话仍需该会话的 workspace 与项目一致。

## 八、我等你决定

只做说明，不执行。你确认后我可以代为执行第三节的三条命令；在此之前共享面板保持原样。关联事件 #598、#611、#613。