"""A durable record of which models this panel may dispatch on.

The restriction the user gave is a standing one — "only Gemini 3.8 Flash" — so
holding it in an environment variable would make it true only for one process
and silently false for the next start. It is written to the data directory
instead, next to the store it governs.

The record stores model ids exactly as the app reports them, together with the
display name seen in the app's own catalogue at the time it was recorded. That
name is kept so a reader can tell what the id meant without re-deriving it, and
so a later mismatch between id and name is visible rather than hidden.
"""

import json
import time
from pathlib import Path

FILENAME = "model-policy.json"


def path_for(data_dir):
    return Path(data_dir) / FILENAME


def load(data_dir):
    """The recorded policy, or None when nothing was ever recorded.

    Three outcomes, deliberately distinct:

    * `None` — no record at all. Nothing was ever decided, so no restriction
      exists and an ordinary session keeps its own model.
    * `{"invalid": ...}` — a record exists but cannot be trusted: bad JSON, a
      non-object, or an `allowed` that is missing, not a list, or empty. This
      must be read as "cannot confirm", never as permission. A file someone
      wrote and then damaged is still evidence that a decision was made, and an
      empty allow-list forbids everything rather than permitting it.
    * a dict — a usable record.
    """
    target = path_for(data_dir)
    if not target.exists():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"invalid": f"无法解析：{exc}", "allowed": []}
    if not isinstance(record, dict):
        return {"invalid": "顶层不是对象", "allowed": []}
    allowed = record.get("allowed")
    if not isinstance(allowed, list):
        return {"invalid": "allowed 缺失或不是列表", "allowed": []}
    if not allowed:
        return {"invalid": "allowed 为空，未声明任何允许的模型", "allowed": []}
    return record


def _scope_matches(record, project):
    """Whether a recorded restriction governs this project.

    A restriction is recorded for a purpose — here, QA model checks — not for the
    whole machine. Applying it to every project would silently bind a user's
    ordinary sessions to a choice they made about testing only. An entry with no
    projects listed is unscoped and governs everything, which is what an
    explicitly global restriction means.
    """
    scoped = record.get("projects")
    if not scoped:
        return True
    if not project:
        return False
    return project.get("id") in scoped


def allowed_ids(data_dir, project=None):
    """The set of permitted model ids for this project, or an empty set.

    Returns None when the record cannot be read, which callers must treat as
    "cannot confirm" — never as permission. A record that does not cover this
    project contributes nothing, so an ordinary session keeps its own model.
    """
    record = load(data_dir)
    if record is None:
        return set()
    if record.get("invalid"):
        return None
    if not _scope_matches(record, project):
        return set()
    return {str(m) for m in (record.get("allowed") or []) if m}


def record(data_dir, models, source="user", projects=None):
    """Write the restriction. `models` maps model id to the app's display name.

    `projects` limits it to named projects; omit it for a machine-wide rule.
    """
    payload = {
        "allowed": sorted(models),
        "display_names": dict(models),
        "projects": list(projects or []),
        "recorded_at": time.time(),
        "recorded_from": source,
        "note": ("面板只会向使用这些模型的会话派发；会话模型无法确认时同样拒绝。"
                 "列入 projects 时仅约束这些项目，留空则约束全部。"),
    }
    target = path_for(data_dir)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(target)
    return payload


def clear(data_dir):
    target = path_for(data_dir)
    if target.exists():
        target.unlink()
        return True
    return False