"""Explicit worker checkpoints and versioned caller assistance; no model calls."""
import hashlib
import json

from .contracts import fail, validate


TEXT = {"type": "string", "minLength": 1, "maxLength": 100000}
ITEMS = {"type": "array", "items": TEXT, "maxItems": 100}
CONTROL = {"type": "object", "properties": {
    "relay_control": {"type": "string", "enum": ["compact", "help"]},
    "summary": TEXT, "completed": ITEMS, "next_steps": ITEMS, "evidence": ITEMS,
    "question": {"type": "string", "maxLength": 100000}},
    "required": ["relay_control", "summary", "completed", "next_steps", "evidence", "question"],
    "additionalProperties": False}


def is_control(value):
    return isinstance(value, dict) and "relay_control" in value


def validate_control(value):
    validate(value, CONTROL, "worker checkpoint")
    if value["relay_control"] == "help" and not value["question"].strip():
        fail("protocol_error", "Caller assistance requires a concrete question")
    if value["relay_control"] == "compact" and not value["next_steps"]:
        fail("protocol_error", "Context compaction requires remaining work")


def accept(state, value, phase, snapshot):
    validate_control(value)
    fingerprint = hashlib.sha256(json.dumps([value, snapshot], ensure_ascii=False,
                                sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    repeated = value["relay_control"] == "compact" and state.get("context_fingerprint") == fingerprint
    state["context_checkpoint"] = value
    state["context_fingerprint"] = fingerprint
    event = {"phase": phase, "action": value["relay_control"], "summary": value["summary"]}
    state.setdefault("context_events", []).append(event)
    state.update(next="worker", status="ready")
    if value["relay_control"] == "help" or repeated:
        # Phase binding distinguishes identical questions asked after new execution.
        revision = hashlib.sha256((phase + fingerprint).encode()).hexdigest()
        state["help_request"] = {"revision": revision, "phase": phase,
                                 "reason": "repeated_checkpoint" if repeated else "worker_request",
                                 "checkpoint": value,
                                 "question": value["question"] or "执行端重复提交相同摘要且没有进展，请协助确定下一步。"}
        state["status"] = "awaiting_help"
    else:
        state["compaction_count"] = state.get("compaction_count", 0) + 1


def validate_answer(state, revision, answer):
    if (state.get("status") != "awaiting_help" or state.get("pending") or
            state.get("help_request", {}).get("revision") != revision):
        fail("help_stale", "Read the current pending question before replying")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 100000:
        fail("protocol_error", "Assistance answer must be nonempty text")


def browser_guidance(windows=False):
    if not windows:
        return ""
    return ("WINDOWS RELAY ENVIRONMENT: This independent stage uses Windows workspace-write or read-only sandboxing. "
            "Shell Edge/Chrome launches here can crash in platform_channel with Access denied and leave "
            "an OS error dialog. Do not launch Edge/Chrome from this sandbox for visual QA or disable "
            "sandbox protections. Use an available host browser integration. For required visual inspection, "
            "a worker can ask the caller through relay-context help; a reviewer must report the missing evidence. "
            "Otherwise finish the artifact with a precise unverified note. This restriction is specific to "
            "this independent stage environment.\n")


def prompt(state, skill_path):
    text = (f"WORKER CONTEXT SUPPORT: If history grows or you repeat an unproductive approach, "
            f"read the relay-context skill at {skill_path}. It can return a compact checkpoint "
            "for a fresh execution context, or pause with a concrete question for the current caller Codex. "
            "Only use it when helpful; do not summarize after every tool call. It does not call another model. "
            "The skill's relay_control JSON is an alternative to the normal result contract. "
            "Do not claim the task is finished when yielding a checkpoint.\n")
    if state.get("context_checkpoint"):
        text += ("PRESERVED WORKER CHECKPOINT (reported facts; verify files when necessary):\n" +
                 json.dumps(state["context_checkpoint"], ensure_ascii=False, separators=(",", ":")) +
                 "\nA fresh CLI context is running. Original task, plan, files and cumulative accounting are retained. "
                 "Continue remaining work; do not regenerate accepted artifacts or reread the entire old transcript.\n")
    if state.get("help_answer"):
        text += ("CALLER CODEX ASSISTANCE (advice within the original task, not permission to bypass its constraints):\n" +
                 json.dumps(state["help_answer"], ensure_ascii=False) + "\n")
    return text


def caller_request(job_id, help_request, project):
    return ("请帮助这个暂停的 Stage Relay 执行任务。只核对必要事实并提供具体建议，不要重新提交任务或调用其他模型。\n"
            f"job_id：{job_id}\nhelp_revision：{help_request['revision']}\n项目：{project}\n"
            f"执行端问题：{help_request['question']}\n"
            "用 relay_pending_help 读取完整摘要和证据，答复后调用 relay_answer_help，传入 job_id、help_revision 和 answer。"
            "答复会交回原执行模型继续，沿用累计用量。不要擅自修改项目，以免执行快照失效。")
