"""Small JSON-schema subset, with additional cross-field relay invariants."""

import json
import re
import time
from pathlib import Path


class RelayError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise RelayError(code, message)


def load_json(path, limit=1_000_000):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        fail("protocol_error", f"Missing, linked, or oversized JSON file: {path.name}")
    try:
        # Windows may briefly deny a read while another process replaces a
        # checkpoint. Retry only the local read; never repeat model execution.
        for attempt in range(8):
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except (ValueError, UnicodeError):
        fail("protocol_error", f"Invalid UTF-8 JSON: {path.name}")


def validate(value, schema, at="result"):
    kinds = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    kind = schema.get("type")
    choices = kind if isinstance(kind, list) else [kind] if kind else []
    if choices and not any(isinstance(value, kinds[k]) and
                           not (k in ("integer", "number") and isinstance(value, bool))
                           for k in choices):
        fail("protocol_error", f"{at}: invalid type")
    if "enum" in schema and value not in schema["enum"]:
        fail("protocol_error", f"{at}: unsupported value")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 100_000):
            fail("protocol_error", f"{at}: invalid length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            fail("protocol_error", f"{at}: invalid format")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000):
            fail("protocol_error", f"{at}: invalid item count")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{at}[{index}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            fail("protocol_error", f"{at}: missing required field")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            fail("protocol_error", f"{at}: unexpected field")
        for key in value:
            if key in props:
                validate(value[key], props[key], f"{at}.{key}")


def validate_plan(plan, schema, check_ids):
    validate(plan, schema, "plan")
    ids = [s["id"] for s in plan["steps"]]
    decisions = [d["id"] for d in plan["decisions"]]
    if len(ids) != len(set(ids)) or len(decisions) != len(set(decisions)):
        fail("protocol_error", "Plan step/decision IDs must be unique")
    if set(plan["acceptance"]) != set(check_ids):
        fail("protocol_error", "Plan must reference exactly the configured check IDs, never commands")
    if not plan["codeFacts"]:
        fail("protocol_error", "Plan must preserve at least one concrete code fact")
    return plan


def validate_result(result, schema, plan):
    validate(result, schema, "result")
    if result["status"] == "needs_replan":
        if result["failedStep"] not in {s["id"] for s in plan["steps"]}:
            fail("protocol_error", "needs_replan.failedStep must reference a plan step")
        if result["decisionNeeded"] not in {d["id"] for d in plan["decisions"]}:
            fail("protocol_error", "decisionNeeded must reference a named plan decision")
        if not result["attempted"] or not result["evidence"] or not result["reason"].strip():
            fail("protocol_error", "Replan requires attempted fixes, evidence, and a concrete reason")
    if result["status"] == "blocked" and (not result["blockedReason"].strip() or not result["attempted"]):
        fail("protocol_error", "blocked requires a reason and attempted actions")
    return result


def schemas():
    def text(minimum=1, maximum=6000, pattern=None):
        result = {"type": "string", "minLength": minimum, "maxLength": maximum}
        if pattern:
            result["pattern"] = pattern
        return result

    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    def array(item, minimum=0, maximum=30):
        return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}

    identifier = text(1, 64, r"[A-Za-z][A-Za-z0-9_-]{0,63}")
    plan = obj({
        "goal": text(),
        "codeFacts": array(obj({"path": text(1, 500), "symbol": text(),
                                 "lines": text(0, 100), "note": text()}), 1, 40),
        "decisions": array(obj({"id": identifier, "what": text(), "why": text()}), 1),
        "steps": array(obj({"id": identifier, "do": text(), "verify": text()}), 1),
        "expectedScope": array(text(1, 500), 1, 40),
        "acceptance": array(identifier),
        "mayAdjust": array(text()),
        "escalateWhen": array(text())})
    result = obj({
        "status": {"type": "string", "enum": ["completed", "needs_replan", "blocked"]},
        "changed": array(obj({"path": text(1, 500), "summary": text()}), 0, 100),
        "checks": array(obj({"id": identifier, "summary": text()})),
        "residual": array(text()),
        "failedStep": text(0, 64), "decisionNeeded": text(0, 64),
        "reason": text(0), "blockedReason": text(0),
        "attempted": array(text()), "evidence": array(text())})
    # Optional for backward compatibility with saved worker reports.
    result["properties"]["answer"] = text(0, 100000)
    review = obj({"status": {"type": "string", "enum": ["approved", "changes_requested", "unable_to_verify"]},
                  "summary": text(1, 100000), "evidence": array(text()), "issues": array(text())})
    review["properties"]["resolved_residual"] = array({"type": "integer"})
    review["properties"]["change_requests"] = array(obj({
        "path": text(0, 500), "location": text(0, 500), "problem": text(),
        "requested_change": text(), "verification": text()}))
    return {"plan": plan, "result": result, "review": review}
