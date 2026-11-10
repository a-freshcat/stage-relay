# 群聊协作缺陷：原线程回复 + 成员移除

板块 `task_ca586a410225`（owner codex，我执行）。本条只做实现与自测，验收由监督者与用户完成。

## 一、缺陷一：只在原生会话回复，不回原线程

### 现状核对

`relay/rules.py` 里原文只有一句「群聊回复 relay_send_message（议题）或 relay_project_message（项目级）」，没有说明「处理完必须回到被点名的那条线程」，也没有说明原生会话的回答不算回复。摘要里更是完全没有这条约束，而唤醒大多数时候只带摘要。

### 改法

`FULL`（第三节）新增：

```
- 在议题里被点名，处理完就用 relay_send_message 回到同一个议题；在项目群聊被点名，
  就用 relay_project_message 回到项目群聊。原生会话里说的话不算回复。
- 即便本轮只是确认收到、没有新进展，也要在原线程给一句简短确认。
- 原生会话的 assistant/message 不会自动转发，因此不主动回复就等于用户没有收到回复。
- 不要为了补回复而重跑已经完成的任务。
- 已有的自主讨论次数上限照旧：被点名不等于可以反复连发，回完本轮就停。
```

`DIGEST` 新增：

```
回复：被点名后处理完必须在原线程回复（议题用 relay_send_message，项目群聊用 relay_project_message），
只确认收到也要回一句；原生会话里的回答不算回复。
```

摘要里加这条是必要的：只写在全文里，绑定后的常规唤醒看不到。

### 明确没有做的

- `assistant/message` **仍然不自动转发**（验收条件第 3 条要求如此）；这里改的是对成员的指令，不是把原生输出镜像过去。
- 自主讨论上限（`_message_position` / `chat-discussion-bound` / `issue-discussion-bound`）**未改动**，新增的「必须回复」不构成放宽。

## 二、缺陷二：能加成员，不能移除

### 现状核对

`_apply` 里只有 `invite` 追加成员，没有任何移除路径。UI 里也只有 `邀请成员` 按钮。

### 改法

新增 `remove_member` 动作（`relay/domain.py`）：

```python
elif action == "remove_member":
    issue = get(p["issues"], a.get("issue_id"), "议题")
    member = get(state["agents"], a.get("agent_id"), "成员")
    if member["id"] not in issue["members"]:
        raise Invalid("此成员不在该议题中。")
    if member["id"] == p["assignments"].get("planner"):
        raise Invalid("不能移除本项目的方案与监督成员，请先调整分工。")
    if len(issue["members"]) <= 1:
        raise Invalid("议题需要至少保留一名成员。")
    issue["members"] = [m for m in issue["members"] if m != member["id"]]
```

三条语义选择：

1. **与 `invite` 对称，同为用户决定**：加入 `user_actions` 集合，成员不能自行踢人。
2. **不动历史消息**：已经发出的消息保留原作者与内容，改写记录等于歪曲当时说了什么。
3. **撤销未派发的唤醒**：`Runtime.cancel_member_issue()` 只把该成员在该议题上 `status='queued'` 的 job 置为 `cancelled`。已 `leased`/`delivered` 的不撤回——那份工作已经在原生应用里，面板无法召回。

### UI

`web/app.js` 新增 `removeMember()` 与 `remove-member` 按钮（议题卡片与群聊抽屉各一处），并在弹窗里写明：已发消息保留、未派发的唤醒撤销、已在原生应用执行的不会被打断。下拉里不列出 planner，避免先选后拒。

## 三、真实操作验证（真实 store）

```
start            ['codex','deepseek','antigravity']
after invite     ['codex','deepseek','antigravity']   （已存在时不重复）
after remove     ['codex','deepseek']
supervisor guard  不能移除本项目的方案与监督成员，请先调整分工。
member guard      这项操作由用户决定。
access guard      请先由用户邀请加入此议题。
```

即：移除生效、supervisor 受保护、成员不能自踢、被移除者随后发言被拒。

## 四、测试

```
tests/test_workflows.py
  test_a_removed_member_loses_access_and_keeps_the_history
  test_the_supervisor_and_the_last_member_cannot_be_removed
  test_removing_a_member_cancels_only_its_undelivered_issue_work
tests/test_context.py
  test_the_full_rules_name_the_thread_to_reply_in
  test_the_digest_also_requires_a_thread_reply
  test_the_discussion_bound_is_still_stated
```

```
python -m pytest tests -q   238 passed, 1 skipped
node qa/ui_probe.mjs        ALL UI CHECKS PASSED
```

## 五、边界

- 不补发历史消息（#550 已核对清楚）；
- 不重复执行已完成任务，也不让成员互相无限回复；
- 未改动 `assistant/message` 转发策略与讨论次数上限；
- 未改其他项目、未重启共享面板。

## 六、交付给谁核验

板块 owner 是 codex，`report` 要求 actor == owner，我无法用它提交，故本条按议题消息交付。关联事件 #560、#611、#613。