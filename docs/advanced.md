# 进阶配置

## 命令行与面板

从已配置的插件目录运行，路径替换为自己的实际路径：

```powershell
python panel.py --config "config.local.json" --runs-root "runs/plugin" --projects-root "C:\work\projects" --open
python relay.py preflight --config "config.local.json" --project "C:\work\projects\example"
python relay.py run --config "config.local.json" --project "C:\work\projects\example" --request "request.md" --run-dir "C:\work\relay-runs\example"
```

面板只监听 `127.0.0.1`，使用随机 URL 路径。轮询读取本地记录，不请求模型。仅查看记录时可省略 `--projects-root`；允许启动任务时需要配置它。移动插件后应重新运行 `configure_plugin.py` 生成绝对路径接入信息。

插件调用方的方案和工具参数见 [MCP 接入](../skills/stage-relay/references/setup.md)。`relay_wait(full_result=true)` 可取得完整执行报告；普通预览省略的内容会标记，阻塞与遗留问题仍保留。

## 接口与密钥

执行器要求 Responses 协议与工具调用支持。仅有 Chat Completions / Anthropic Messages 的接口需要先适配。接口模型列表不证明生成、工具调用或实际模型身份。

面板可使用 API Key、已有 Codex provider 或高级环境变量凭据。例：

```json
{"base_url":"https://your-gateway.example/v1","api_key_env":"YOUR_GATEWAY_KEY"}
```

在其他 PowerShell 窗口临时输入环境变量，不会自动让已运行的桌面客户端继承。Windows 普通用户建议使用面板保存密钥，JSON 仅保存凭据引用。替换或删除旧密钥后，引用它的历史任务可能需要重新配置。

原生登录透传可使用 `config.ccswitch-native.example.json`，仅允许用户自己的本地 loopback 网关；Relay 不读取或改写 `auth.json`。服务进程存在不等于对应端口已经监听。`preflight` 只检查 TCP 连通，不验证认证、模型或工具能力。

## 技能发现与模型识别

`worker.skill_reuse` 可省略，默认 `auto`：

| 值 | 行为 |
| --- | --- |
| `auto` | 识别 DeepSeek、GLM、Qwen、Kimi、Claude、Gemini、MiniMax、Mistral、Llama、Grok 等非 GPT 名称并提供本机目录；未知别名跳过 |
| `off` | 不添加额外目录；不禁用 CLI 原生技能 |
| `non_gpt` | 用户已确认该别名是非 GPT 时启用；显式 GPT / Codex 名称和原生登录路线仍始终跳过 |

判断依赖配置中的模型 ID，无法验证中转站实际路由。GPT / Codex 执行端完全跳过新增目录扫描及提示词；规划端、审核端和格式修复也不添加这层目录。它们原本的技能由 Codex 自身管理。

发现范围：项目到 Git 根目录的 `.agents/skills`、用户 `~/.agents/skills`、`$CODEX_HOME/skills`（默认 `~/.codex/skills`）、显式 `skills.config` 路径，以及本机 Codex 配置里已启用插件的缓存技能目录。支持常见字符串和多行简介，不能识别的元数据会给出提示。不会从任意目录、网络或其他对话中收集技能。

同名本地技能优先项目版本；插件技能保留插件命名空间。禁用的技能路径与插件会跳过。插件缓存根据版本号选择最新版本，并尊重清单中的技能路径；这不是桌面应用的活动版本查询，可能与尚未重新连接的会话不同。符号链接和非标准清单可能无法完整识别，界面不把发现数量当成完整性证明。

每个任务生成一次 `worker-skills.json`，其中只有目录元数据和原文件路径；新上下文、返修沿用该目录，下一任务重新扫描。推荐最多提供八项简介，不限制模型读取目录中的其他技能；不把全文或全部技能复制进提示词，也不改动 CLI 原生加载规则。

依赖提示来自技能声明及工具引用的静态检查。服务已配置不等于连接成功；没有提示也不证明技能无需依赖。工具权限、审批、任务范围和输出 JSON 契约继续适用。执行模型在选择技能后应检查实际工具；缺少必要能力时使用 `relay-context` 求助。

读取证据来自主执行 CLI 中直接包含技能完整路径、成功退出的读取命令。相对路径、脚本内部读取、子 agent 读取等可能未记录；这不是完整审计，也不证明技能所有步骤都已执行。运行中只读取最近一段日志，完成阶段保存读取命令记录。

## 检查、恢复和用量

`checks` 是检查 ID 到参数数组的映射，直接启动程序，不隐式使用 shell。计划只能引用这些 ID，不能把模型生成的命令加入调度器白名单。Windows `.cmd` 需要显式选择解释器或实际可执行文件。

正式规划、执行和审核的 `timeout_seconds` 衡量连续无进程输出或模型接口数据的等待时长；持续有活动不会仅因累计时长达到该值而停止。独立兼容检查仍按固定总时长计时。超时或日志超限会停止阶段进程树。

`max_requests`、修复次数、重规划次数等继承用户配置。累计请求在转发前记账，恢复不清零。`max_request_bytes` 默认 `null`，不附加模型请求字节上限；供应商上下文和本地资源限制仍存在。不配置 `max_output_tokens` 时不注入输出上限。

缺少完整价格、用量和所有计价项时，插件不估算严格费用预算。需要实际金额硬限额时，应由供应商或网关实施。父子请求的上游用量不与 CLI 用量相加。

中断后显式恢复会保留现有改动并传递检查点，要求执行模型先检查文件再继续；不会清理、撤销或重复生成已接受产物。它是新 CLI 阶段，不是原会话的逐 token 续接。项目锁只协调本工具，外部编辑仍可能使快照失效。

自动返修遇到相同问题且结果无变化时暂停，证据不足不会继续猜测。审核回复绑定执行结果版本，过期回复不能关闭新问题。执行端求助使用 `relay_pending_help` / `relay_answer_help`；旧连接的本地 CLI 回退见面板复制的请求。

## 运行环境

独立 CLI 没有天然继承当前桌面对话的全部工具。Windows 受限执行环境曾出现 Edge 的进程通信崩溃，独立阶段不会从该沙箱尝试启动 Edge/Chrome。必要视觉检查交给有工具的当前 Codex；可选核验缺失时仍完成产物并写明范围。

面板应由正常用户进程启动，模型子进程仍保持原审批和沙箱。如果 CLI 在请求模型前初始化失败，先查看 `cli_initialization_error` 的诊断，勿通过关闭保护或搬动认证文件处理。

## 开发验证与打包

```powershell
python -m unittest discover -s tests
python tests/cli_protocol_smoke.py --skills
python tests/cli_protocol_smoke.py --context-help
python build_plugin.py
```

CLI smoke 使用已安装的真实 Codex CLI 对接本地假 Responses 服务，无付费模型调用。它验证调用链路与文件读取，不证明真实供应商模型能自主正确选择技能，也不证明所有技能依赖可用。

包体只包含源码、界面、插件自己的技能、测试和文档，不包含使用者的技能、密钥、配置、运行目录或本机接线。压缩包中的 `.mcp.json` 是空模板，使用前在本机生成。
