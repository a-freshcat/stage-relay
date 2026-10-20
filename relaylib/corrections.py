"""Durable review feedback and correction rounds; no model calls here."""
import hashlib
import json
from pathlib import Path
from .contracts import load_json
from .runtime import atomic_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def feedback(review):
    return {k: review[k] for k in ("summary", "issues", "change_requests", "evidence") if k in review}


def work_signature(state):
    checks = [{k: c.get(k) for k in ("id", "exit_code", "failure", "stdout_tail", "stderr_tail")}
              for c in state.get("validation") or []]
    return digest({"snapshot": state["snapshot"], "answer": (state.get("last_result") or {}).get("answer"), "checks": checks})


def round_path(directory, number):
    return Path(directory) / "corrections" / (f"{number:06d}.json")


def update_round(directory, state, **changes):
    number = state.get("correction_round", 0)
    path = round_path(directory, number)
    if number and path.is_file():
        value = load_json(path)
        value.update(changes)
        atomic_json(path, value)


def history(directory):
    result = []
    for path in sorted((Path(directory) / "corrections").glob("[0-9]*.json")):
        result.append(load_json(path))
    return result


def public_cycle(state):
    cycle = state.get("review_cycle", {})
    return {k: cycle[k] for k in ("active", "kind", "auto_correct", "paused", "reason") if k in cycle}
