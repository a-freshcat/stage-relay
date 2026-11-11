# Stage Relay 部署执行记录：b8adb36

执行时间：2026-11-11T19:3x Z
执行者：DSH（deepseek），经用户 #804 明确批准、Codex #805 指定为唯一执行者
状态：**已完成**

---

## 一、结果摘要

```
旧 pid      16308   version 0.1.4   antigravity.online = False
                               reason = 端口 61271 连接被拒
新 pid      27268   version 0.1.4   antigravity.online = True
                               reason = 已连接原生应用
                               last_error = None
```

同一 data-dir、同一端口，唯一变量是代码版本。`online` 由 False 变为 True，即端口重发现在真实实例上生效。

---

## 二、执行前的实际状态

```
GET /api/health       pid 16308, version 0.1.4
                      data_dir = ...\Stage Relay\data
                      antigravity.online = false
命令行                python app.py --open      ← 无 --port，无 --data-dir
ExecutablePath        C:\Python314\python.exe
```

与方案记录一致，未按旧信息盲停。

### 在途检查（全项目 / 全成员）

```
queued      codex     delivery_04fc711117f1 ev806   -> 后被领取，执行前复查已不在队列
queued      deepseek  delivery_77536e2befd7         -> 保持 queued
leased      无
uncertain   无
```

判定：**无 leased / uncertain**，`queued` 只存在于库里，重启后 poll 会重新领取，安全。执行前做了第二次复查，结论不变。

心跳：`deepseek 0.9s` / `codex 0.3s` 活跃；`antigravity 9316s` 离线，与 `cascades=0` 一致。

---

## 三、备份

```
workspace.sqlite3.before-b8adb36.bak
  方式   sqlite3.backup()（在线一致性快照，非文件复制）
  大小   16,990,208 字节
  校验   PRAGMA integrity_check = ok
         tables 14 / events 807 / jobs 443 / bindings 4

connection-ownership.json.before-b8adb36.bak
  大小   1160 字节
```

备份首次校验 events 807，执行时源库已到 809（期间有事件写入），属正常推进，不是备份缺陷。

不备份 `connection-ownership.json` 会在新实例启动后触发「同名配置已被其他操作修改」，已一并保留。

---

## 四、部署的代码

```
commit   b8adb36
目录     release\StageRelay-0.3.1-codex-dsh\deploy-b8adb36
来源     git archive b8adb36   （不是工作树目录）
```

固定检出内已逐项确认：

```
_policy_project absent       True
_port_answers (重发现)       True
DISCOVERY_COOLDOWN           True
_discovery_reason (接线)     True
encoding="utf-8" (解码)      True
discovery_failure (诊断)     True
```

未混入共享工作树中他人的未提交改动（`tests/test_cleanup.py`、`test_storage.py`、`test_transport.py` 的 `addCleanup` 重构，以及未提交的 `tests/test_qa_lifecycle.py`）。

---

## 五、启停

```
停止   Stop-Process -Id 16308      -> 已停止，端口 4318 释放
启动   C:\Python314\python.exe app.py --port 4318 --data-dir "<原 data-dir>"
       工作目录 deploy-b8adb36
       不带 --open
       日志  ...\Stage Relay\panel-4318-b8adb36.log / .err
```

端口与 data-dir 均显式写出，不依赖默认值。未带 `--open`，避免端口被占时静默打开旧实例页面。

未关闭 Antigravity、未关闭 DSH 应用、未改任何其他应用配置、未跑向导。

---

## 六、执行后核验

```
pid           27268
version       0.1.4
data_dir      原 data-dir
online        True
cascades      0
delivered     0
last_error    None
busy          []

绑定（4 条，全部保留）
  project_4d8879b7ef2c  antigravity  db46248f-901d-4154-9b3c-9c99a6d3e888
  project_4d8879b7ef2c  codex        01a1c42e-32d9-7a61-905a-4095e5d286a3
  project_4d8879b7ef2c  deepseek     session-1f337fbf-6a43-46cb-9c17-0ac21274cee6
  project_7cf085ba6b15  antigravity  650c185b-2fa3-4211-9977-655df638b1e8

数据
  integrity   ok
  events      809
  jobs        443
```

`cascades=0` 表示当前被发现的 LS 返回 0 个会话。这不是「所有持久会话都不存在」，只是此刻可见集合为空。

antigravity 在主项目上最近的任务保持原状（`delivered` / `coalesced`），未重发、未改状态、未重绑。

---

## 七、回退（本轮未使用）

代码可回退，**数据库不回退**：

```
1) Stop-Process -Id 27268
2) 从所需历史 commit 检出（如 rollback\stage-relay-add8dab）
3) 同一 --port 4318 --data-dir 启动
```

本轮改动未新增表、未改 `workspace` blob 结构，旧代码读新库正常。`.bak` 仅在数据真的损坏时手工使用——覆盖会丢弃备份之后的所有事件，**不自动执行**。

---

## 八、未做的事

- 未派模型任务、未消耗额度；
- 未重发已接受或结果未知的任务；
- 未删除任何项目或证据；
- 未改 `~/.gemini` 下任何配置；
- 未自动批准任何原生权限请求。