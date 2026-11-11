# Stage Relay 单次部署方案（待用户批准）

生成时间：2026-11-11T19:20Z 左右
提出者：DSH（deepseek）
状态：**已备妥，未执行**。面板未重启、未关闭任何应用、未派模型任务。

---

## 一、要部署的固定版本

```
commit   b8adb36  state what closing really does to sessions, since it archives and unbinds them
```

包含本轮四项修复，均可独立回退：

| commit | 内容 |
| --- | --- |
| `afb9a75` | `AntigravityLoop.connect()` 旧端口失效时重新发现，不再永久断连 |
| `cf21691` | PowerShell 进程查询显式 UTF-8 解码；查询失败不再吞成「没有进程」 |
| `e62450f` | 探测失败原因接入 `state.reason` / `last_error`；10 秒重探测冷却；错误不含认证参数 |
| `b8adb36` | 回收确认文案如实说明结束会归档/解绑会话 |
| `b151a18` / `c06bcb0` | 项目列表回收入口；回收失败时展示真实已结束状态并允许重试 |

### 为什么不直接部署工作树

当前工作树有 **65 项未提交改动**，其中 `web/`、`relay/` 之外的部分**不是本轮工作**：

```
M .gitignore, README.md
M qa/*（多个探针文件）
M tests/test_cleanup.py, tests/test_storage.py, tests/test_transport.py   ← addCleanup 重构，来源不是我
?? tests/test_qa_lifecycle.py                                              ← 未提交的新测试
```

`tests/` 那几处是共享工作树里别人的在途改动。**部署应取 `git archive b8adb36`，不要用工作树目录**，否则会把未核验的 QA 生命周期改动带进运行实例。

---

## 二、干净检出验证（已做）

```
git archive b8adb36 -> 临时目录
python -m pytest tests -q    275 passed, 1 skipped
```

275 而非 277，是因为干净检出不含别人未提交的两个测试；不是回归。

### 隔离启动验证（已做，未触及真实实例）

```
副本数据：  %TEMP%\sr-probe-data-*   （sqlite3.backup() 自真实 store）
端口：      4328（不是 4318）
命令：      python app.py --port 4328 --data-dir "<副本>"
结果：      pid=30028  version=0.1.4
            antigravity.online = True
            reason = 已连接原生应用
            last_error 键存在
```

**这条最重要**：新代码在真实机器上把 `online=False` 变成了 `True`，说明端口重发现确实修好了当前 4318 的断连。副本已删除，真实实例全程未被触碰。

---

## 三、当前 4318 实例的真实身份

```
pid        16308
解释器     C:\Python314\python.exe
命令行     python app.py --open          ← 没有 --port，没有 --data-dir
version    0.1.4
data-dir   C:\Users\handsomeboys\Documents\ChatGPT\new_subagent\release\StageRelay-0.3.1-codex-dsh\Stage Relay\data
状态       antigravity.online = False
            reason = 原生应用未响应：读不到 Antigravity 页面（端口 61271）：连接被拒
```

两点必须写进启动命令：

1. 现在的命令行**完全依赖默认值**才落到 4318 和那个 data-dir。默认值恰好等于真实值，但这不是显式声明。重启时必须把 `--port 4318` 和 `--data-dir` 都写出来，避免默认值变化把实例带到别处或读到别的库。
2. `--open` 会在端口被占用时打开旧实例的页面而不报错，本轮启动**不要带 `--open`**，改为启动后手工确认。

---

## 四、在途任务处置

重启前必须确认无在途投递，否则会留下 `leased` 悬挂：

```
SELECT id,event,status FROM native_jobs
 WHERE project='project_4d8879b7ef2c' AND agent='antigravity'
   AND status IN ('queued','leased','uncertain');
```

判定规则：

- 有 `leased` / `uncertain` → **不重启**，等它落定或先报告实际状态；
- 只有 `queued` → 状态在库里，重启后 `poll` 会重新领取，安全；
- 当前 antigravity **无在线会话**（LS 返回 0），因此本轮没有可投递对象，expect 空集。

**实际测量（2026-11-11T19:20Z）：**

```
主项目 + antigravity，status IN (queued, leased, uncertain)  ->  none
```

因此本次重启不会留下悬挂投递。这一点是执行前必须**重新确认**的：如果届时查询非空，先停下报告。

当前三处绑定（重启后应由 `native_bindings` 保持，不得改变）：

```
project_4d8879b7ef2c  antigravity  db46248f-901d-4154-9b3c-9c99a6d3e888
project_4d8879b7ef2c  codex        01a1c42e-32d9-7a61-905a-4095e5d286a3
project_4d8879b7ef2c  deepseek     session-1f337fbf-6a43-46cb-9c17-0ac21274cee6
```

不重发已 acceptance/已 delivered 的任务，不清 `busy`，不重绑。

---

## 五、SQLite 一致备份

**执行前基线（供回滚后比对）：**

```
PRAGMA integrity_check  ->  ok
events                  796
native_jobs             434
```

```python
import sqlite3
s = sqlite3.connect(f'file:{src}?mode=ro', uri=True)   # 只读打开，不改源库
d = sqlite3.connect(dst); s.backup(d)                  # 在线备份 API，不是复制文件
```

为什么不用 `Copy-Item`：WAL 模式下直接复制文件可能拿到撕裂的库。`sqlite3.backup()` 走一致性快照。

落盘位置与校验：

```
<data-dir>\..\workspace.sqlite3.before-b8adb36.bak
PRAGMA integrity_check  ->  ok
记录备份时的 events / native_jobs 行数，供回滚后比对
```

同样备份 `connection-ownership.json`（保留当前 `owned` 三元组，否则启动后会报「同名配置已被其他操作修改」）。

备份文件**不纳入 git**，也不删除旧备份。

---

## 六、启动命令

```powershell
# 1) 停旧实例（仅这一个 PID，确认它仍是 16308 才停）
Stop-Process -Id 16308

# 2) 从固定检出启动，显式声明端口与 data-dir
cd "<b8adb36 检出目录>"
C:\Python314\python.exe app.py `
  --port 4318 `
  --data-dir "C:\Users\handsomeboys\Documents\ChatGPT\new_subagent\release\StageRelay-0.3.1-codex-dsh\Stage Relay\data" `
  *> "<data-dir>\..\panel-4318.log"

# 3) 启动后核对
GET http://127.0.0.1:4318/api/health
  期望 pid 变化
  期望 antigravity.online = true
  期望 antigravity.reason = 已连接原生应用
  期望 last_error 为空
```

启动**不关闭 Antigravity**、不改其配置、不跑向导、不碰其他应用。

---

## 七、代码回退（不回滚数据库）

本次部署的代码是可回退的，数据库**不需要**跟着回退：

```
1) 停掉 b8adb36 实例
2) 从 rollback\stage-relay-add8dab （或需要的任意历史 commit）检出旧代码
3) 用同一 --port 4318 --data-dir 启动
```

数据库向前兼容：本轮的改动没有新增表、没有改 `workspace` blob 结构、没有加必填字段，只有 `native_resources` 的 `origin` 语义沿用既有值。旧代码读新库不会报错。

若确实需要回到备份（仅在数据被破坏时）：

```
停止实例 -> 把 .bak 放回 workspace.sqlite3 路径 -> 启动
```

这会**丢弃备份之后的所有事件**，因此只在损坏时使用，正常回退不碰数据。

---

## 八、执行前后的边界

- 需要用户**明确的一次性授权**才能执行第四节到第七节；
- 不套用 #672（已用完）或 #773（DSH 重启专用）的授权；
- 执行前若 PID 已变化或有会话在途，先报告实际状态再等指示，不按旧 PID 盲停；
- 原生权限弹窗由用户决定，不代批。

---

## 九、未做的事

- 未停任何进程、未重启面板；
- 未删除任何项目、未清 QA 证据；
- 未派模型任务、未消耗额度；
- 未改 `~/.gemini` 下任何配置。