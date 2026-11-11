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

    A missing file and an unreadable file are different answers: missing means no
    policy, unreadable means this panel cannot vouch for what is allowed and the
    caller must treat the answer as unknown rather than as permission.
    """
    target = path_for(data_dir)
    if not target.exists():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"unreadable": True, "allowed": []}
    if not isinstance(record, dict):
        return {"unreadable": True, "allowed": []}
    allowed = record.get("allowed") or []
    if not isinstance(allowed, list):
        return {"unreadable": True, "allowed": []}
    return record


def allowed_ids(data_dir):
    """The set of permitted model ids, or an empty set when unrestricted.

    An unreadable record returns None, which callers must read as "cannot
    confirm" — never as "everything is fine".
    """
    record = load(data_dir)
    if record is None:
        return set()
    if record.get("unreadable"):
        return None
    return {str(m) for m in (record.get("allowed") or []) if m}


def record(data_dir, models, source="user"):
    """Write the restriction. `models` maps model id to the app's display name."""
    payload = {
        "allowed": sorted(models),
        "display_names": dict(models),
        "recorded_at": time.time(),
        "recorded_from": source,
        "note": "面板只会向使用这些模型的会话派发；会话模型无法确认时同样拒绝。",
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