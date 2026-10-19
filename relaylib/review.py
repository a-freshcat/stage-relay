"""Bind a planner's review to the exact saved execution and check evidence."""
import hashlib
import json
import time

from .contracts import fail, load_json, schemas, validate
from .runtime import atomic_json


def target_fingerprint(spec):
    value = {key: spec[key] for key in ("model", "endpoint")}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def revision(directory, state):
    value = {key: state.get(key) for key in ("last_result", "validation", "snapshot")}
    value["plan"] = load_json(directory / "plan.json")
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_review(directory, state):
    path = directory / "review.json"
    if not path.is_file():
        return None
    value = load_json(path)
    value["stale"] = value.get("result_revision") != revision(directory, state)
    return value


def read_confirmation(directory, state):
    path = directory / "confirmation.json"
    if not path.is_file():
        return None
    value = load_json(path)
    value["stale"] = value.get("result_revision") != revision(directory, state)
    return value


def save_review(directory, state, expected_revision, review, reviewer, selection=None):
    validate(review, schemas()["review"], "review")
    if not state.get("last_result") or expected_revision != revision(directory, state):
        fail("review_stale", "Execution evidence changed; read the result again before reviewing")
    if review["status"] == "approved" and review["issues"]:
        fail("protocol_error", "An approved review cannot contain unresolved issues")
    if review["status"] == "approved" and not review["evidence"]:
        fail("protocol_error", "An approved review requires verification evidence")
    if review["status"] == "changes_requested" and not review["issues"]:
        fail("protocol_error", "A changes_requested review must identify actionable issues")
    resolved = review.get("resolved_residual", [])
    residual = (state.get("last_result") or {}).get("residual", [])
    if len(set(resolved)) != len(resolved) or any(i < 0 or i >= len(residual) for i in resolved):
        fail("protocol_error", "resolved_residual must contain unique indexes of saved worker notices")
    if resolved and not review["evidence"]:
        fail("protocol_error", "Resolving a notice requires the planner's verification evidence")
    value = {**review, "reviewer": reviewer, "time": time.time(), "result_revision": expected_revision}
    if selection:
        value["reviewer_selection"] = selection
    atomic_json(directory / "review.json", value)
    return value
