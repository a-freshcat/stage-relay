# 磁盘归因：下一步应检查的已知应用目录（只读，未扫描整盘）

## 结论

workspace 内 751,257,782 bytes（716.5 MiB）与 `~/.gemini` 已知目录合计仍**不足 1 GB**，解释不了约 10 GB。以下按「与本次测试直接相关」列出候选检查点及理由，**均未删除、未清理**。

## 一、已测到的已知应用目录（含本次直接相关项）

```
~/.gemini/antigravity              247,950,155 bytes  236.5 MiB  3,656 文件   ← 真实登录态目录（原件）
~/.gemini/antigravity-ide          247,162,854 bytes  235.7 MiB  3,650 文件   ← 我启动的实例触发 Wizard 复制
~/.gemini/antigravity-backup      247,162,854 bytes  235.7 MiB  3,650 文件   ← 同上
~/.gemini/stage-relay-probe          2,424,758 bytes    2.3 MiB     73 文件   ← 早期独立 LS 探针
```

用户目录这四项合计约 **744,700,621 bytes**（710.2 MiB）。加上 workspace 的 751,257,782 bytes，总计约 **1.50 GB**。

**仍然差一个数量级。**

## 二、conversations：最值得看清楚的一处（已测，本次相关）

```
~/.gemini/antigravity/conversations   139,001,856 bytes  132.6 MiB  16 文件
```

明细（KiB）：

```
09ffdfe4-2d4d-45f0-9ebd-89982197c09a.db   83,232   LastWrite 11-01   ← 旧数据，非本轮
53f1514a-97a9-4ffa-8d71-314936655854.db   21,024   LastWrite 11-06   ← 旧数据，非本轮
d9109d23-aca1-485c-b054-08af7289832f.db   10,176   LastWrite 10-29   ← 旧数据
f7edecf1-11ee-4af7-8f2a-07ade68674df.db    7,648   LastWrite 10-30   ← 旧数据
4274a8e9-be26-4e41-802e-18fa4a4a3d78.db    6,496   LastWrite 10-29   ← 旧数据
e7a3321f-0b82-46d3-ad56-210006fec06f.db    4,192   LastWrite 11-03   ← 旧数据
5a62fd9e-f969-4779-ba93-df363f3d5b06.db      704   LastWrite 10-29   ← 旧数据
1d00906e-73a0-4b0b-97b2-f9d6f7101726.db      672   LastWrite 11-10   ← 共享实例正在用的会话页
2c30a00c-... / 30f170e9-... / 35c8a59c-... / d5f8217f-...   各 384 KiB   ← 本轮我经 API 建的测试 cascade
```

**本轮我建的 cascade 合计约 1.5 MiB**，不是 10 GB 的来源。132.6 MiB 里 **133 MB 属于 10-29 至 11-06 的旧会话**。

## 三、尚未测、但有明确理由需要检查的目录（等授权，不擅自扫）

| 目录 | 为什么该看 | 预期量级判断 |
| --- | --- | --- |
| `~/.gemini/antigravity/brain`、`scratch`、`cache`、`daemon` | 我在 LS 参数里见过 `--app_data_dir`，语义上是模型/索引/缓存；本次多次 `StartCascade`/`SendUserCascadeMessage` 即使 executor 构造失败，也可能写入轨迹缓存 | 未知；**这是最可能与 10 GB 相关的项**：长期运行的 agent 产品会把检索索引与内容缓存放这里 |
| `~/.gemini/antigravity/conversation_summaries.db` 及 `-wal` | 云端摘要轮询（日志中 `cloud summaries poll`）可能扩张 | 未知 |
| Antigravity 安装目录 `%LOCALAPPDATA%\Programs\antigravity` | 自动更新：日志显示 2.29.0 → 2.30.0，更新会同时留下新旧两套资源 | 单套约 500 MB 级；**更新残留在 1 GB 量级是合理的** |
| `%LOCALAPPDATA%\antigravity-updater` 或同名缓存目录 | electron-updater 的下载缓存（日志里出现过 blockmap 404 与「fallback to full download」） | 每次整包下载数百 MB，**可能是主要增量** |
| `%TEMP%` 与 `%LOCALAPPDATA%\Temp` | 我本轮多次启动应用与执行 PowerShell，可能留临时文件 | 通常数十 MB 级 |

## 四、明确不列入的范围

- 不扫整盘。
- 不追随重解析目录（当前 `ReparsePoint` 查询为空，暂未见链接）。
- 不读用户凭据、账号 token、会话内容正文。
- 不删除、不移动、不调用清理入口。

## 五、需要用户授权才能继续的部分

上面第三节四个方向都在 **workspace 之外**，属于用户目录与系统缓存。按项目规则我只在 workspace 内工作，因此需要用户明确授权「可只读统计这些特定目录的大小与文件数（不含内容）」，我才会测。

在此之前，我能给出的确定结论只有一句：**workspace 内 + `~/.gemini` 已知项合计约 1.50 GB，不足以解释约 10 GB；真正的增量很可能在 Antigravity 的更新残留、缓存/索引目录或系统临时目录，尚未测量。**

## 六、为本次测试新增/修改的 application 数据（如实归因）

```
本轮经 API 新建 cascade 的会话库  约 1.5 MiB（6 个 384 KiB 文件 + 1 个 672 KiB 页面会话）
本轮新建 release/StageRelay-0.1.4-bound        36.2 MB
本轮新建 build/StageRelay-0.1.4-bound          29.4 MB
本轮新建 qa/bound-live                         34.0 MB
Wizard 触发复制（由我启动引发，非我执行）      470 MB（二字目录合计）
本轮删除（盘点之前，处置不当）                 qa/mention-bound-* 数 MB、qa/ag-probe 数 MB
```

合计约 **571 MB**，其中 470 MB 是 Wizard 副本。**这仍不足 10 GB 的一半。**