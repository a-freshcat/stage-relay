"""Native Codex delegation policy and public lifecycle records. No model calls here."""
import json
import os
import tomllib
from pathlib import Path, PurePosixPath

from .contracts import fail


SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "mode": {"type": "string", "enum": ["off", "auto", "manual"]},
    "max_concurrent": {"type": "integer", "minimum": 1, "maximum": 100},
    "tasks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
        "properties": {"goal": {"type": "string", "minLength": 1},
                       "access": {"type": "string", "enum": ["read", "write"]},
                       "files": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}},
        "required": ["goal", "access", "files"]}}}, "required": ["mode"]}


def policy(value=None, project=None):
    from .contracts import validate
    value = {"mode": "off"} if value is None else value
    validate(value, SCHEMA, "subagents")
    tasks, writes = value.get("tasks", []), []
    if (value["mode"] == "manual") != bool(tasks):
        fail("subagents_config", "指定分工需要至少一项任务；其他模式请移除指定分工。")
    for task in tasks:
        if not task["goal"].strip():
            fail("subagents_config", "请填写子任务目标。")
        for name in task["files"]:
            path = PurePosixPath(name.replace("\\", "/"))
            if not name.strip() or path.is_absolute() or ".." in path.parts or ":" in name or str(path) == ".":
                fail("subagents_config", "子任务文件必须是项目内的相对路径，不能使用上级目录。")
            if project and not (Path(project) / str(path)).resolve().is_relative_to(Path(project).resolve()):
                fail("subagents_config", "子任务文件不能通过链接指向项目以外。")
            if task["access"] == "write":
                key = str(path).casefold().rstrip("/")
                if any(key == old or key.startswith(old + "/") or old.startswith(key + "/") for old in writes):
                    fail("subagents_config", "写入文件范围重复；请为各子任务分配互不重叠的文件。")
                writes.append(key)
    return json.loads(json.dumps(value))


def public_agent_id(headers):
    import re
    identity = headers.get("session_id") or headers.get("conversation_id") or ""
    try:
        metadata = headers.get("x-codex-turn-metadata", "")
        if len(metadata) <= 64_000:
            identity = json.loads(metadata).get("thread_id") or identity
    except (ValueError, TypeError, AttributeError):
        pass
    return identity if isinstance(identity, str) and re.fullmatch(r"[A-Fa-f0-9-]{32,36}", identity) else None


def cli_options(value, model, enabled=True):
    value = policy(value)
    active = enabled and value["mode"] != "off"
    options = ["-c", "features.multi_agent=" + str(active).lower()]
    if active:
        # The CLI validates an explicit default against its GPT catalog, even
        # when the same third-party model runs successfully as the parent.
        # Replace only the agents table: omit role/model overrides so children
        # inherit the parent's model, effort, provider and permissions.
        native = {}
        try:
            native = tomllib.loads((Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml").read_text(encoding="utf-8")).get("agents", {})
        except (OSError, ValueError):
            pass
        settings = {"enabled": True}
        for key in ("max_threads", "max_concurrent_threads_per_session"):
            if type(native.get(key)) is int and native[key] > 0:
                settings[key] = native[key]
        if "max_concurrent" in value:
            settings.pop("max_threads", None)
            settings["max_concurrent_threads_per_session"] = value["max_concurrent"]
        options += ["-c", "agents={" + ",".join(k + "=" + str(v).lower() for k, v in settings.items()) + "}"]
    else:
        options += ["-c", "agents.enabled=false"]
    return options


def instructions(value, manual_completed=False):
    value = policy(value)
    if value["mode"] == "off":
        return "Do not spawn subagents.\n"
    text = ("The user explicitly authorizes native subagents for this execution stage. "
            "Use the existing plan; do not call a separate planner or other models. "
            "Children MUST inherit your model and provider: omit model and reasoning_effort overrides. "
            "Give each child a concrete goal, relevant facts, verification method and explicit file ownership. "
            "Prefer fresh context with a compact handoff, without hiding necessary evidence. "
            "Delegate only independent useful work; do not duplicate delegated work. "
            "Keep immediate critical-path work local. Never edit files owned by an active child. "
            "Children must not spawn further children. Read-only assignments are instructions, not separate "
            "permission sandboxes; all children inherit the parent permissions. "
            "Inspect child results, integrate changes and truthfully report unresolved problems. "
            "Wait for every child, collect its answer, and close every child BEFORE returning the final JSON. "
            "On child failure, report or resolve it; never claim its work passed.\n")
    if value["mode"] == "auto":
        text += "AUTO: decide whether delegation will help; small or dependent tasks should stay local.\n"
    else:
        text += ("MANUAL: the previous checkpoint already finished and closed these assignments. "
                 "Reuse their reported evidence; do not respawn them just to satisfy a count. "
                 "Only delegate necessary remaining work within the same assignments.\n" if manual_completed else
                 "MANUAL: use exactly these delegated assignments; keep their write sets disjoint.\n")
        text += json.dumps(value["tasks"], ensure_ascii=False) + "\n"
    return text


def lifecycle(path, model=None):
    """Read the full bounded phase log, retaining results across wait and close events."""
    rows, calls = {}, {}
    if not Path(path).is_file():
        return {"agents": [], "unfinished": [], "failed_calls": []}
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item", {})
            if item.get("type") != "collab_tool_call":
                continue
            calls[item.get("id")] = item
            for identity in item.get("receiver_thread_ids", []):
                row = rows.setdefault(identity, {"id": identity, "model": model, "status": "unknown", "answer": "", "goal": ""})
                if item.get("tool") in ("resume_agent", "resume", "send_input", "send_message", "followup_task") and item.get("status") == "completed":
                    row["closed"] = False
                if item.get("tool") == "spawn_agent" and item.get("prompt"):
                    row["goal"] = item["prompt"]
                state = item.get("agents_states", {}).get(identity, {})
                if state.get("status"):
                    row["status"] = state["status"]
                if state.get("message"):
                    row["answer"] = state["message"]
                if item.get("tool") == "close_agent" and item.get("status") == "completed":
                    row["closed"] = True
    terminal = {"completed", "errored", "shutdown", "not_found", "notFound"}
    return {"agents": list(rows.values()),
            "unfinished": [r["id"] for r in rows.values() if not r.get("closed") or r["status"] not in terminal],
            "failed_calls": [c.get("tool") for c in calls.values() if c.get("status") in ("failed", "interrupted", "in_progress")]}


class UsageCollector:
    """Bound telemetry only; never truncate or alter the forwarded response."""
    def __init__(self, content_type):
        self.sse = "text/event-stream" in content_type
        self.buffer = b""
        self.usage = None
        self.disabled = False

    def feed(self, chunk):
        if self.disabled:
            return
        self.buffer += chunk
        if self.sse:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:"):
                    self.parse(line[5:].strip())
        if len(self.buffer) > 4_000_000:
            self.disabled, self.buffer = True, b""

    def parse(self, raw):
        try:
            obj = json.loads(raw)
            response = obj.get("response", {}) if self.sse else obj
            if self.sse and obj.get("type") != "response.completed":
                return
            usage = response.get("usage")
            if not isinstance(usage, dict):
                return
            result = {k: usage.get(k) for k in ("input_tokens", "output_tokens")}
            result["cached_input_tokens"] = (usage.get("input_tokens_details") or {}).get("cached_tokens")
            if all(type(result[k]) is int and result[k] >= 0 for k in ("input_tokens", "output_tokens")):
                self.usage = result
        except (ValueError, TypeError, AttributeError):
            pass

    def finish(self):
        if not self.sse and not self.disabled:
            self.parse(self.buffer)
        return self.usage


def routed_usage(path, phase=None):
    requests, reports = {}, {}
    if Path(path).is_file():
        with Path(path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if phase and row.get("phase") != phase:
                    continue
                identity = row.get("request_id")
                if identity and row.get("event") == "request_forwarded":
                    requests[identity] = row.get("agent_id")
                elif identity and row.get("event") == "response_usage":
                    reports[identity] = row.get("usage")
    def total(ids):
        complete = bool(ids) and all(isinstance(reports.get(i), dict) for i in ids)
        return {"requests": len(ids), "complete": complete,
                **{k: sum(reports[i][k] for i in ids) if complete and all(type(reports[i].get(k)) is int for i in ids) else None
                   for k in ("input_tokens", "output_tokens", "cached_input_tokens")}}
    return {**total(list(requests)), "agents": {a: total([i for i in requests if requests[i] == a]) for a in set(requests.values()) if a},
            "source": "upstream Responses usage; not added to CLI usage", "cost_usd": None}
