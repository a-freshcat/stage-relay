"""Bind a planner's review to the exact saved execution and check evidence."""
import hashlib
import json
import time

from .contracts import fail, load_json, schemas, validate
from .runtime import atomic_json


def target_fingerprint(spec):
    value = {key: spec[key] for key in ("model", "endpoint")}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate_correction_review(review):
    if review.get("status") == "changes_requested":
        if not review.get("change_requests"):
            fail("protocol_error", "Automatic corrections require change_requests with location, problem, requested_change and verification")
        for item in review["change_requests"]:
            if any(not item.get(key, "").strip() for key in ("problem", "requested_change", "verification")):
                fail("protocol_error", "Every requested change needs a concrete problem, change and verification method")


def revision(directory, state):
    value = {key: state.get(key) for key in ("last_result", "validation", "snapshot")}
    if "result_generation" in state:
        value["result_generation"] = state["result_generation"]
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


def save_review(directory, state, expected_revision, review, reviewer, selection=None, source="api"):
    validate(review, schemas()["review"], "review")
    if not state.get("last_result") or expected_revision != revision(directory, state):
        fail("review_stale", "Execution evidence changed; read the result again before reviewing")
    if review["status"] == "approved" and review["issues"]:
        fail("protocol_error", "An approved review cannot contain unresolved issues")
    if review["status"] == "approved" and not review["evidence"]:
        fail("protocol_error", "An approved review requires verification evidence")
    if review["status"] == "changes_requested" and not review["issues"]:
        fail("protocol_error", "A changes_requested review must identify actionable issues")
    if review["status"] != "changes_requested" and review.get("change_requests"):
        fail("protocol_error", "Only changes_requested reviews can request implementation changes")
    resolved = review.get("resolved_residual", [])
    residual = (state.get("last_result") or {}).get("residual", [])
    if len(set(resolved)) != len(resolved) or any(i < 0 or i >= len(residual) for i in resolved):
        fail("protocol_error", "resolved_residual must contain unique indexes of saved worker notices")
    if resolved and not review["evidence"]:
        fail("protocol_error", "Resolving a notice requires the planner's verification evidence")
    value = {**review, "reviewer": reviewer, "source": source, "time": time.time(), "result_revision": expected_revision}
    if selection:
        value["reviewer_selection"] = selection
    identity = hashlib.sha256(json.dumps({k: v for k, v in value.items() if k != "time"}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    value["id"] = identity
    history = directory / "reviews"
    history.mkdir(exist_ok=True)
    record = history / (identity[:24] + ".json")
    if record.is_file():
        value = load_json(record)
        if value.get("id") != identity:
            fail("protocol_error", "Review history identifier collision")
    else:
        atomic_json(record, value)
    atomic_json(directory / "review.json", value)
    return value
