# 面板加载新代码：可审查的部署方案（返修版）

按 #644 的五条返修重写。原文的「四行」不成立，不要照抄。

## 零、这份文档没有做的事

- 没有结束任何进程；
- 没有启动任何新进程；
- 没有写真实数据库。

## 一、重启前必须复核在途执行（#644 第①条）

原方案用「`antigravity` 键不存在」推断「没有 loop 在跑」，这只证明旧服务没有这个功能，
**不能证明 Codex / DSH 没在执行**。实测数据：

```
=== /api/health ===
version 0.1.4, pid 26284, antigravity 键不存在        ← 只说明旧代码

=== native_jobs 在途（status in queued/leased/delivered/uncertain）===
antigravity  delivered 3
antigravity  queued    1
codex        delivered 141
deepseek     delivered 115

=== 最新 job 时间 ===
delivery_a237d788233a  deepseek  delivered  age 91 s      ← 刚刚，正在跑的就是它
delivery_23de0be823df  codex     delivered  age 27961 s   （约 7.8 小时前）

=== native_hosts 心跳 ===
deepseek   seen age  0.8 s
codex      seen age  0.0 s
antigravity seen age 32166.8 s                          （约 8.9 小时前）
```

结论：**deepseek 与 codex 是活的**（心跳 0–1 秒），antigravity 的最后活动在 9 小时前，
它的 `queued` 那条也是 9 小时前的遗留，不是正在执行。

`delivered` 这个状态在 `native_jobs` 里表示「应用已接受，可能仍在执行」，
所以计数本身不能当作「正在跑」。真正的判据是 **host 心跳 + activity recency**。

复核命令（只读）：

```powershell
cd C:\Users\handsomeboys\Documents\ChatGPT\new_subagent
python -c "import sys,time;sys.path.insert(0,'.');from pathlib import Path;from relay.domain import Store;s=Store(Path(r'release\StageRelay-0.3.1-codex-dsh\Stage Relay\data\workspace.sqlite3'));c=s.connect().__enter__();[print(r[0],'seen age',round(time.time()-r[1],1),'s') for r in c.execute('SELECT agent,seen FROM native_hosts')]"
```

重启前置条件，**全部成立才可继续**：

1. 没有一个 agent 的 `seen age` 小于 30 秒且在跑工具（例外：执行本次操作的成员自己）；
2. 没有 `status='leased'` 的 job；
3. `status='delivered'` 且 `updated` 在 5 分钟内的 job 为 0（正在跑的会持续续租）。

第 3 条正是这次会失败的检查：执行本方案的那一刻，我自己的 job 就是最近的 `delivered`。
因此**重启不能在对话进行中执行**，要等这一轮结束后、由用户单独发起。

## 二、一致性备份（#644 第②条）

`Copy-Item` 对在写的 SQLite 不是一致性备份（可能复制到半页）。改用 SQLite 备份 API，
并在备份后校验。已实际执行过一次：

```powershell
python -c "import sqlite3;src=r'release\StageRelay-0.3.1-codex-dsh\Stage Relay\data\workspace.sqlite3';s=sqlite3.connect(f'file:{src}?mode=ro',uri=True);d=sqlite3.connect(src+'.before-add8dab.bak');s.backup(d);d.close();s.close()"
```

本次结果：

```
-wal present: False    -shm present: False
db size:      14,401,536
backup:       workspace.sqlite3.before-add8dab.bak  14,401,536
source:  integrity_check=ok  tables=14  events=645
backup:  integrity_check=ok  tables=14  events=645
```

原文件与 WAL/SHM 都保留，未删除、未改写。

恢复时不要直接覆盖：先在另一个数据目录用 `--data-dir` 指向备份副本启动一次，
确认能读到同样的项目与事件数，再决定是否替换。这样不会用旧数据掩盖新记录。

## 三、启动命令必须写明（#644 第③条）

原方案写 `python app.py --open` + 「cd 到目录」，依赖默认目录。实测默认值恰好指向同一个
store，但这是巧合，不能依赖。改为显式全参数：

```powershell
$root   = 'C:\Users\handsomeboys\Documents\ChatGPT\new_subagent'
$python = 'C:\Python314\python.exe'
$data   = 'C:\Users\handsomeboys\Documents\ChatGPT\new_subagent\release\StageRelay-0.3.1-codex-dsh\Stage Relay\data'

& $python "$root\app.py" --port 4318 --data-dir $data
```

三个参数都必须是绝对值：

- `--python`：`C:\Python314\python.exe`，即正在运行那个面板的解释器（`ExecutablePath` 实测）；
- `--data-dir`：必须是当前 store，否则界面会显示另一套（或空）项目；
- `--port 4318`：固定，避免落到别的端口后用户找不到。

不加 `--open` 时不会自动开浏览器，需要时手动打开 `http://127.0.0.1:4318`。

## 四、回滚目标必须是真的可运行版本（#644 第④条）

原方案的 `git checkout -- relay/` 只丢弃工作树改动，**不动 HEAD**；`40c6129~1`
也从未被验证过能否启动。二者都不能称为「可用回滚」。

已建立固定回滚目录，内容取自 `add8dab` 的 git archive，并实测可导入：

```powershell
$rb = 'C:\Users\handsomeboys\Documents\ChatGPT\new_subagent\rollback\stage-relay-add8dab'
# 验证
& C:\Python314\python.exe -c "import sys;sys.path.insert(0,'$rb');from relay import __version__;print(__version__)"
```

实测输出：

```
version 0.1.4
has reload_owned: True
```

回滚执行方式（与启动同一形状，只换根目录）：

```powershell
& $python "$rb\app.py" --port 4318 --data-dir $data
```

回滚**不回写数据库**。旧代码自己会读同一个 store；如果旧代码无法理解新写入的记录，
那属于真实的不兼容，应当记录并报告，不能用恢复备份来掩盖。

## 五、结束共享面板需要用户明确同意（#644 第⑤条）

本方案不代为执行 `Stop-Process`。这一条不是我能在这一轮里「顺手做完」的事，
它需要用户在看到上述前置检查结果后单独同意。

## 六、已授权并已完成的改动

`relay/httpd.py::main` 的端口占用提示，已按 #644 授权直接改：

```
Stage Relay 已在运行：http://127.0.0.1:4318
  该实例 pid 26284，版本 0.1.4，加载的是它自己启动时的代码。
  本次未启动新版本；这里只是打开既有服务。
  如需加载更新的代码，请先结束 pid 26284 再启动。
```

这条不再只是「已在运行」，而是明确说明本次没有启动新版本。

## 七、完整执行顺序（等用户批准后）

```
1. 复核第一节三段检查，全部通过
2. 用 SQLite backup 生成新备份并校验 integrity / tables / events
3. 结束 pid 26284（用户在同意时给出这一个 pid，不批量结束）
4. 用第三节的三参数命令启动
5. 判定：health 的 pid 不再是 26284，且出现 antigravity 键
6. 若第 5 步失败：用第四节的回滚目录原样启动，数据库不动
```

## 八、仍未解决、与本方案无关的

- 新建 cascade 首轮 `400 GenerateContentRequest.contents is not specified`；
- `create_session` 对 antigravity 保持拒绝，UI 也如此显示。

关联事件 #624、#627、#628、#631、#633、#639、#644。