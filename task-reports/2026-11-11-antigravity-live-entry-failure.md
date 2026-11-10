# 用户实际入口故障：两个真实原因

用户 #624/#627 报告重启后仍无法接入，弹窗为「暂时无法自动接入 / 检测到同名配置已被其他操作修改，为保留现有配置，请重新核对。」本节给出可复现的根因，不是推测。

## 根因一：`python app.py --open` 在端口占用时不会启动新代码

`relay/httpd.py::main` 的实际路径：

```python
try:
    server = PanelServer(("127.0.0.1", args.port), store, connection_home=args.app_home)
except OSError:                       # 端口已被占用
    ... 读 /api/health 确认是 Stage Relay ...
    print(f"Stage Relay 已在运行：http://127.0.0.1:{args.port}")
    if args.open:
        webbrowser.open(url)          # 只把浏览器指向旧服务
    return                            # ← 直接返回，不启动任何新进程
```

所以用户“重开应用”时，如果旧后台进程还活着，用户看到的只是**旧进程的界面**。这与用户感知的“我重启了”完全冲突，但代码确实这样做。

实测支持：

```
pid 26284  python app.py --open   CreationDate 2026-11-10 17:49:49
```

Codex #628 也读到同一 pid 26284，说明从 17:49 到 19:42 之间用户的多次“重启”都没换进程。

这是**正确的保护**（防止两个进程在同一端口发布不同数据目录），但缺少一条出口：端口被占用时没有告诉用户“这是旧代码，需要先退出旧实例”。可以改进的措辞：

```
当前输出：Stage Relay 已在运行：http://127.0.0.1:4318
建议改为：Stage Relay 已在运行（pid NNNN，版本 X.Y.Z）。
          如果刚更新了代码，需要先结束该进程再启动；本次不会启动新版本。
```

我不擅自改这句，因为它涉及用户可见行为，等指示。

## 根因二：`Connections.owned` 只在构造时读一次磁盘（已修）

这才是弹窗的直接来源。`relay/connections.py` 原来：

```python
self.journal = self.data_dir / "connection-ownership.json"
if self.journal.exists():
    self.owned = json.loads(...)      # 读一次，此后进程内永不刷新
```

流程：

```
1. pid 26284 于 17:49:49 启动，此时 journal 只有 deepseek / codex
2. 我在 20:xx 用一次性入口完成接管，journal 写入 antigravity（磁盘已正确）
3. 用户点“接入”，旧进程用内存里的 owned（无 antigravity）比较：
      current  = 磁盘上的 stage-relay-antigravity 条目（已接管的新值）
      owned    = None
      current != owned  →  raise Invalid("检测到同名配置已被其他操作修改…")
```

实测确认磁盘与条目完全一致：

```
owned after reload: ['antigravity', 'codex', 'deepseek']
current == owned:   True
```

即：**磁盘没问题，旧进程内存有问题**。

### 修法

```python
def reload_owned(self):
    """磁盘 journal 是共享记录，读它、不要缓存整个进程生命周期。"""
    self.owned = json.loads(self.journal.read_text(...)) if self.journal.exists() else {}
```

并在 `prepare()` 开头调用。理由：一个面板可能在一个由**另一个进程**完成的采用之后继续运行——一次性维护入口、或同一 store 上的新版面板。把首次读到的内容当作永久事实，会让合法采用被误判为他人修改，用户看到的是无法通过重试清除的冲突提示。

测试 `test_ownership_written_by_another_process_is_seen_without_restart`：另一个 manager 先 adopt，本 manager 保留旧视图，`prepare` 仍成功且写入正确条目。

```
tests/test_connections.py   16 passed
```

## 为什么这条修复仍不足以让用户界面变好

`reload_owned` 是**代码改动**，而 pid 26284 跑的是旧代码。所以：

```
新代码里：弹窗不会 occor
旧进程里：仍然弹
```

用户要看到改善，必须先退出 pid 26284。这回到根因一。

## 可直接执行的修复（按顺序）

```powershell
# 1) 只读确认是旧代码
python -c "import json,urllib.request;h=json.load(urllib.request.urlopen('http://127.0.0.1:4318/api/health'));print(h['pid'],h['version'],'antigravity' in h)"

# 2) 备份
$d="C:\Users\handsomeboys\Documents\ChatGPT\new_subagent\release\StageRelay-0.3.1-codex-dsh\Stage Relay\data"
Copy-Item "$d\workspace.sqlite3" "$d\workspace.sqlite3.before-69ecbe6.bak"
Copy-Item "$d\connection-ownership.json" "$d\connection-ownership.json.before-69ecbe6.bak"

# 3) 结束旧进程（只这一个）
Stop-Process -Id 26284 -Force

# 4) 同目录、同命令启动
cd C:\Users\handsomeboys\Documents\ChatGPT\new_subagent
python app.py --open
```

判定成功：`h['pid']` 不再是 26284，且出现 `antigravity` 键。

回滚见 `task-reports/2026-11-11-antigravity-panel-deploy-and-rollback.md`。

## 现场其他进程（只读观察，未处置）

```
26284  python app.py --open                       面板（旧代码）
4772   bridge.py --agent deepseek --port 4318     面板子进程
32468  bridge.py --agent codex --port 4318        面板子进程
1788   bridge.py ...                               （父 7164）
12960  bridge.py ... （父 24760 = language_server）Antigravity 侧桥
25692  bridge.py ... （父 7164）
```

`12960` 挂 `language_server` 下，说明 Antigravity 确实在通过接管的配置启动桥——这部分是好的。

## 报告文件归属

`task-reports/2026-11-11-antigravity-existing-session-acceptance.md` 被我上一轮 `write` 覆盖成部署方案。git 显示该文件是 `69ecbe6` 新建的（128 insertions、0 deletions），即 Codex 的版本只在工作树存在，未提交，无法从 git 恢复。

已按 #633 处理：把我的部署方案移到独立文件 `2026-11-11-antigravity-panel-deploy-and-rollback.md`，并删除被覆盖的那个文件名，避免继续占用 Codex 的路径。这是我的操作失误，记录在此。关联事件 #624、#627、#628、#631、#633。