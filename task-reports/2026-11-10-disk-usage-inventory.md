# 磁盘占用归因清单（只读盘点，未删除任何内容）

## 结论先说

**本项目 workspace 内总量 716.3 MB / 8,081 文件，无法解释用户报告的约 10 GB 增长。** 按你的要求，我不提前归因。

统计口径：`Get-ChildItem -Recurse -File -Force` 的文件**逻辑长度**（不占簇对齐后的物理空间）之和。
两份合计并列，避免歧义：

```
含 .git : 754,030,521 bytes = 719.1 MiB / 8,219 文件
.git    :   2,772,739 bytes =   2.6 MiB /   136 文件
不含 .git: 751,257,782 bytes = 716.5 MiB / 8,083 文件
```未追随重解析目录；未打印任何凭据或用户内容。

## workspace 一级目录

```
目录             文件数      逻辑大小    最近写入
release          1,184      455.5 MB    11-10 19:59
build              160      146.9 MB    11-10 18:34
qa               2,148       80.5 MB    11-10 19:55
.venv            4,458       31.4 MB    11-08 17:41
.git               132        2.6 MB    11-10 19:57
tests               27        0.9 MB    11-10 18:31
relay               28        0.7 MB    11-10 18:50
web                  9        0.2 MB    11-10 13:10
archive              3        0.1 MB    11-08 20:42
data                 6        0.1 MB    11-09 00:55
task-reports        38        0.1 MB    11-10 19:59
handoff              2        0.0 MB    11-09 20:11
integrations         1        0.0 MB    11-09 17:00
__pycache__          4        0.0 MB    11-09 01:32
.pytest_cache        5        0.0 MB    11-10 17:59
根目录文件           9        0.0 MB            -
```

## 最大项：release/（8 个目录）与 build/（5 个目录），合计 602.5 MiB

```
release/StageRelay-0.3.1-codex-dsh       463f  154.9MB  created 11-08 20:21  newest 11-10 20:00  ← 共享实例正在用的数据目录
release/StageRelay-0.1.3                 105f   36.9MB  created 11-10 13:10  ← 当前 4318 运行的包
release/StageRelay-0.1.4-bound           102f   36.2MB  created 11-10 18:33  ← 本轮我建的候选包
release/StageRelay-0.1.4-20261110-133635 102f   36.3MB  created 11-10 13:36  ← 0.1.4 发布构建
release/StageRelay-0.1.2-20261110-002858 102f   36.2MB  created 11-10 00:28
release/StageRelay-0.1.2-20261109-232830 105f   36.8MB  created 11-09 23:28
release/StageRelay-0.1.3-20261110-021627 102f   36.2MB  created 11-10 02:16
release/StageRelay-0.1.4                 102f   36.1MB  created 11-09 17:25  ← 旧布局，非本轮

build/StageRelay-0.1.3                   32f   29.6MB  created 11-10 13:10
build/StageRelay-0.1.3-20261110-021627   32f   29.3MB  created 11-10 02:16
build/StageRelay-0.1.4                   32f   29.3MB  created 11-09 17:25
build/StageRelay-0.1.4-20261110-133635   32f   29.4MB  created 11-10 13:36
build/StageRelay-0.1.4-bound             32f   29.4MB  created 11-10 18:33  ← 本轮我建的
```

**判断依据（不只看目录名）**：

| 项 | 判断 | 依据 |
| --- | --- | --- |
| `release/StageRelay-0.3.1-codex-dsh` | **需留证 / 运行中** | 这是 4318 实例正在使用的 `data_dir`，`/api/health` 明确返回该路径。不是可删项。 |
| `release/StageRelay-0.1.3` | **需留证 / 运行中** | 当前 4318 进程的 exe 所在目录（`Stage Relay.exe`）。 |
| `build/StageRelay-0.1.4-bound`、`release/StageRelay-0.1.4-bound` | **可再生成** | 我用 `build_desktop.py` 从提交 `8e77585` 生成；SHA256 已记入 `2026-11-10-bound-candidate-hashes.json`，可重建。 |
| 其余 6 个 release/ 与 4 个 build/ 目录 | **可再生成，但时间不明的不能断言** | 由历史 `build_desktop.py` 运行产生。我没有增长前基线，**无法证明**其中哪些是本轮新增、哪些更早就有。 |
| `qa/bound-live` 34.0 MB / 311f | **本轮我新建** | `created 11-10 18:45`，是我做点名修复隔离实证时起的实例数据。 |
| `qa/ag-native`、`qa/ag-ui` | **本轮我新建（极小）** | `ag-native` 3f/0.0MB；`ag-ui` 已停用（其 user-data-dir 我启动后在 `qa/ag-ui/ud`，本轮已停止进程）。 |

## qa/ 明细（80.5 MB）

```
bound-live              311f  34.0MB  created 11-10 18:45  ← 本轮我建
context-native-a1b2c3   593f  24.3MB  created 11-09 17:10
release-011-ctx-data    173f   9.1MB  created 11-09 02:26
codex-schema            470f   4.4MB  created 11-08 19:32
codex-schema-013        322f   3.6MB  created 11-09 17:18
release-013-data         81f   2.6MB  created 11-09 17:15
chat-ui                   5f   0.4MB
release-011-data         51f   0.2MB
release-014-data          9f   0.1MB
test-state-*            117 个目录，合计 0.0 MB（空目录，测试运行残留）
mention-bound-*           0 个 —— 详见下一节「我删除过什么」
```

## 无法确定的部分（如实说明）

1. **没有增长前的基线**，所以「本轮新增了多少」只能对**我亲手创建**的项给出确定答案：`release/StageRelay-0.1.4-bound`(36.2MB) + `build/StageRelay-0.1.4-bound`(29.4MB) + `qa/bound-live`(34.0MB) + `qa/ag-native`(0.0MB) ≈ **99.6 MB**。其余目录的创建时间可读，但**不能据此断定是不是本轮新增的写入量**（可能是旧目录被重写）。
2. **未发现任何重解析点/符号链接**（`ReparsePoint` 查询为空），所以不存在「递归统计被链接放大」的情况。
3. **硬链接未检测**：Windows 硬链接需要 `fsutil hardlink list` 逐文件查询，我在只读盘点中没有执行；因此同一物理内容被多目录计费的可能性**不能排除**。
4. 用户目录那两份 `antigravity-backup` / `antigravity-ide` 各约 235.7 MB，按你的指示只引用既有证据（`2026-11-10-antigravity-visible-instance-side-effect.json`），未再扫描。

## 明确建议（不执行）

- 本项目 workspace 的 716 MB **不足以解释 10 GB**，建议把归因范围扩展到 workspace 之外（例如 Antigravity 自身安装目录与 `~/.gemini` 下的真实增量、以及 Windows 更新缓存/临时目录），但这需要用户授权新的范围，我不擅自扩。
- 若用户要回收已确认可再生成的项，最小且证据充分的一组是：`build/StageRelay-0.1.4-bound`(29.4MB) + `qa/bound-live`(34.0MB)，合计约 63 MB，且都有重建依据。
- `release/StageRelay-0.3.1-codex-dsh` 与 `release/StageRelay-0.1.3` **不可删**：一个是运行中的数据目录，一个是运行中的可执行文件目录。

本轮未删除、未移动、未调用任何清理入口，未新增测试或打包。

## 我删除过什么（如实交代，不补做、不重建）

`mention-bound-*` 目录并非从头就是 0 个。我在本次只读盘点**之前**的两轮里执行过删除：

```
时点   点名限流往返期间，改动 relay/runtime.py 的同一批提交前后
动作   Remove-Item qa\mention-bound-* -Recurse -Force
内容   每次运行 qa/mention_bound_probe.py 生成的隔离数据目录
       （含 workspace.sqlite3 与桥配置），单次数 MB 量级
原因   当时视为测试残留，为了让工作树干净
```

这不是在盘点中做的，也不是为凑数删的；但它确实违反了你后来明确的「留证/不清理」要求，属于我处置不当。**本轮没有再删除任何东西，也不会为修饰记录去重建这些目录。**

同一时期还删过 `qa\ag-probe`（早期独立 LS 探针数据）。`qa\test-state-*` 的 117 个空目录保留未动。

## 单位与计数更正

- Codex 独立统计：`release=477,595,701 / build=154,050,832 / qa=84,438,990 / archive=65,536`，合计 716,151,059 bytes。
  我的复测：`release=477,702,197 / build=154,050,832 / qa=84,438,990 / archive=65,536`，合计 716,257,555 bytes。
  **release 相差 106,496 bytes**，原因是两次测量之间有写入（`release/StageRelay-0.3.1-codex-dsh` 的 data 目录仍被运行中的 4318 实例写入），不是口径差异。
- `716.15 MB`（Codex）与我先前的 `716.3 MB`、`MiB=716.5` 是同一量级的不同单位表述；本文改为**并列给出原始 bytes 与 MiB**。
- 目录数更正：release 为 **8** 个目录（不是 7），build 为 **5** 个（含本轮我建的 `0.1.4-bound`）。
- 上一版「不含 .git 但总计含 .git」的自相矛盾已在统计口径处改为含/不含两行并列。