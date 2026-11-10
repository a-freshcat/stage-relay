# Antigravity MCP 接管方案（可审查，未写入真实配置）

## 现状（只读实测）

配置文件：

```
C:\Users\handsomeboys\.gemini\config\mcp_config.json
```

同名条目当前内容：

```json
{
  "command": "C:\\Users\\handsomeboys\\Documents\\ChatGPT\\new_subagent\\release\\StageRelay-0.1.0\\Stage Relay\\StageRelayBridge.exe",
  "args": ["--agent","antigravity","--port","4318",
           "--data-dir","C:\\Users\\handsomeboys\\Documents\\ChatGPT\\new_subagent\\release\\StageRelay-0.1.0\\Stage Relay\\data"]
}
```

`command` 指向的 `StageRelayBridge.exe` **不存在**（0.1.0 目录已删除）。所以 Antigravity 现在起不来桥，这是路径失效，不是权限问题。

## 将写入的条目（`Connections.current_entry` 实测输出，不是拼接猜测）

```json
{
  "command": "C:\\Python314\\python.exe",
  "args": ["C:\\Users\\handsomeboys\\Documents\\ChatGPT\\new_subagent\\bridge.py",
           "--agent","antigravity","--port","4318",
           "--data-dir","C:\\Users\\handsomeboys\\Documents\\ChatGPT\\new_subagent\\release\\StageRelay-0.3.1-codex-dsh\\Stage Relay\\data"]
}
```

说明两点，避免误判：

1. **不是 exe 前缀**。本机当前运行的是开发态实例，`current_entry` 走的是 `sys.executable + bridge.py` 分支；Codex 已独立核实 `C:\Python314\python.exe` 与 `bridge.py` 都存在。构造目标必须调用 `Connections.current_entry`，不能假设「把 exe 名换掉」即可。
2. `--port 4318` 与 `--data-dir` 指向**当前运行实例**的 store，与 deepseek / codex 两个成员用的是同一个 store。

## 接管机制（已实现，`relay/connections.py`）

原来的行为：磁盘上存在同名条目但不在 `owned` 记录里 → 直接拒绝。这是对的，但会让失效条目永远无法被合法修正。

新增的是**显式采用**，不是放宽检查：

```python
if current is not None and current != owned:
    if adopt is None:
        raise Invalid("检测到同名配置已被其他操作修改，为保留现有配置，请重新核对。")
    if adopt.get("server") != server:
        raise Invalid("接管确认的条目名与目标不一致，未修改文件。")
    if adopt.get("digest") != sha256(canonical_json(current)):
        raise Invalid("磁盘上的同名配置与核对时不一致，请重新核对；尚未写入内容。")
```

即调用方必须提交**它核对过的那一条的确切内容摘要**。效果：

- 不能用「同意」这种笼统确认；
- 不能顺手接管别的 server；
- 核对之后文件被人改过就失效（摘要不匹配）；
- 题述条目以外的 `mcpServers` 成员一个都不动。

本次的采用摘要（对上述现状内容取规范化 JSON 的 SHA256）：

```
bc656696fdbc3ef89b0c35457d0572a735825907a35bf16e6de808f623f3e8ba
```

## 已有的安全措施（沿用，未改动）

- `prepare` 不写盘，只产出计划；
- `commit` 要求显式同意，且再次比对文件未变，否则拒绝；
- 写前生成 `.stage-relay-<ns>.bak` 完整备份；
- 临时文件 + 原子替换，失败不破坏原文件；
- 符号链接/重定向目标直接拒绝；
- 备份文件保留，可回滚；
- 其他 `mcpServers` 保持原样（有测试）。

## 测试锁定

`tests/test_connections.py`：

```
test_unowned_or_modified_entries_are_never_replaced   保留：无摘要一律拒绝
test_an_unowned_entry_is_only_replaced_with_an_exact_adoption
  ├ 错误的 server 名 → 拒绝
  ├ 过期摘要（0*64）→ 拒绝
  └ 正确摘要 → 接受，且 other 服务保持原样
```

全套：`201 passed, 1 skipped`；`node qa/ui_probe.mjs` 全过。

## 回滚路径

1. `commit` 产生的 `mcp_config.json.stage-relay-<ns>.bak` 是写入前的完整原文，直接覆盖回去即恢复；
2. 面板的撤销（`remove=true`）只移除本面板登记的那一条，其余不动；
3. 若接管后发现不可用，也可只删 `stage-relay-antigravity` 一项，`github-mcp-server` 不受影响。

## 未做、等用户决定的

- **没有写入真实配置**；
- 未重启共享应用；
- 未代为同意任何权限请求；
- 未替 Antigravity 生成任何协作回执。

用户对议题 `issue_b7195aca21b8` 选择「接管旧条目」后，执行顺序为：
`prepare(adopt=上面摘要)` → 展示 diff → `commit(consent=true)` → 用真实应用验证 Antigravity 能启动该 MCP 桥 → 让专用测试会话实际调用 `relay_inbox`，保留工具名/请求/结果 → 交 Codex 独立读回。