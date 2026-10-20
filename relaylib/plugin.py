"""Small caller handoffs, durable jobs and bounded results for local MCP clients."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .contracts import RelayError, fail, load_json, validate
from .runner import ROOT, Runner, TERMINAL, read_config
from .diagnostics import describe_failure
from .runtime import FileLock, atomic_json
from .review import read_review, read_confirmation, revision as review_revision, save_review, target_fingerprint
from .review import validate_correction_review
from .contracts import schemas
from . import subagents
from . import corrections
from . import drafts


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def string(maximum=6000):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def array(item, minimum=0, maximum=30):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


HANDOFF = obj({"goal": string(),
               "files": array(obj({"path": string(500), "note": string()}), 1, 40),
               "steps": array(string(), 1),
               "decisions": array(string(), 1),
               "constraints": array(string())}, ["goal", "files", "steps"])
JOB_ID = {"type": "string", "pattern": "[a-f0-9]{24}"}
TOOLS = [
    {"name": "relay_open_panel", "description": "Bring the exact user_request into the GUI and open its URL first. In the SAME invocation, continue planning as the current Codex: inspect relevant facts read-only, then call relay_prepare with the returned project/invocation_id. Stop when the saved plan is ready for confirmation; no separate follow-up is required. Preparation must not implement the task, generate deliverables, edit task files, run implementation commands or call relay_submit. Execution requires the user's Start click or separate authorization. Honor an explicit open-only request. Omit project for a new standalone artifact; specify project for existing code. This tool makes no model calls.",
     "inputSchema": obj({"open_browser": {"type": "boolean"}, "user_request": string(100000), "project": string(1000)}, []),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "relay_prepare", "description": "After opening the task panel, save the current Codex's plan in the SAME invocation; no extra user message is required. Include the original user_request, actual requirements and invocation_id from relay_open_panel. End after the plan is ready for user confirmation. Do not implement deliverables or submit execution. For new outputs use generated README.md as a facts anchor and name future output paths in steps. Local only, no model call.",
     "inputSchema": obj({"project": string(1000), "plan": HANDOFF,
                         "user_request": string(100000), "instructions": array(string()), "invocation_id": JOB_ID}, ["project", "plan"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "relay_submit", "description": "Execute an existing concise plan only when the user explicitly authorizes execution; a request to open/refine/prepare the panel is not authorization. Preserve user_request. Set review_mode=user_choice to let the user confirm the answer or request original-planner review; auto requests a review after execution. Same project/task_key is idempotent.",
     "inputSchema": obj({"project": string(1000), "task_key": {"type": "string", "pattern": "[A-Za-z0-9_-]{1,64}"},
                         "plan": HANDOFF, "user_request": string(100000),
                         "review_mode": {"type": "string", "enum": ["user_choice", "auto"]},
                         "subagents": subagents.SCHEMA, "draft_id": JOB_ID}, ["project", "task_key", "plan"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}},
    {"name": "relay_wait", "description": "Wait locally up to 50 seconds. Returns a compact result; pass after_revision to suppress unchanged content. No model calls.",
     "inputSchema": obj({"job_id": JOB_ID, "seconds": {"type": "integer"}, "after_revision": string(64),
                         "full_result": {"type": "boolean"}}, ["job_id"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "relay_record_review", "description": "Record this Codex conversation's evidence-based review. changes_requested queues the original execution model to fix issues when auto_correct=true (default), potentially incurring provider usage; after correction this conversation must review again. Supply concrete change_requests and the current result_revision. Approval/insufficient-evidence records do not call models. Stale evidence is rejected.",
     "inputSchema": obj({"job_id": JOB_ID, "result_revision": string(64), "review": schemas()["review"],
                         "auto_correct": {"type": "boolean"}}, ["job_id", "result_revision", "review"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "relay_correct", "description": "Explicitly continue a rejected review or paused correction cycle with the original worker and same reviewer. Preserves edits and cumulative budgets; may incur model usage.",
     "inputSchema": obj({"job_id": JOB_ID, "result_revision": string(64)}),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "relay_pause_corrections", "description": "Request a pause before the next model stage of a correction cycle. The current stage may finish and be billed. Local flag only; no new model call.",
     "inputSchema": obj({"job_id": JOB_ID}),
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}},
    {"name": "relay_resume", "description": "Explicitly recover a stopped job, or provide a revised plan for awaiting_plan. Retains cumulative request budget.",
     "inputSchema": obj({"job_id": JOB_ID, "plan": HANDOFF, "retry_interrupted": {"type": "boolean"}}, ["job_id"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}},
]


def expand_plan(handoff, checks):
    validate(handoff, HANDOFF, "handoff")
    decisions = [{"id": f"D{i+1}", "what": d, "why": "Caller decision"}
                 for i, d in enumerate(handoff.get("decisions", ["Follow the supplied steps"]))]
    decisions.extend({"id": f"C{i+1}", "what": constraint, "why": "Caller requirement"}
                     for i, constraint in enumerate(handoff.get("constraints", [])))
    if len(decisions) > 30:
        fail("protocol_error", "Plan schema supports at most 30 decisions and constraints combined")
    return {"goal": handoff["goal"],
            "codeFacts": [{"path": f["path"], "note": f["note"], "symbol": "caller fact", "lines": ""} for f in handoff["files"]],
            "decisions": decisions,
            "steps": [{"id": f"S{i+1}", "do": s, "verify": "Use configured acceptance checks"}
                      for i, s in enumerate(handoff["steps"])],
            "expectedScope": list(dict.fromkeys(f["path"] for f in handoff["files"])),
            "acceptance": list(checks), "mayAdjust": [],
            "escalateWhen": ["A named caller decision cannot satisfy the goal"]}


def execution_config(path):
    """Inherit operator limits without imposing a second token-saving policy."""
    config = copy.deepcopy(read_config(path))
    # Caller-plan routing is enforced by Runner.plan_source, not a reduced budget.
    return config


def usage_report(directory):
    """Include failed stages; missing CLI accounting stays unknown."""
    reports, incomplete = [], False
    for phase in sorted(p for p in directory.glob("[0-9]*-*") if p.is_dir() and p.name.rsplit("-", 1)[-1] in ("planner", "worker", "reviewer")):
        if not (phase / "finished.json").is_file():
            incomplete = True
            continue
        report = load_json(phase / "finished.json").get("usage", {}).get("usage", [])
        if not report:
            incomplete = True
        reports.extend(report)
    totals = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        values = [r.get(key) if isinstance(r, dict) else None for r in reports]
        totals[key] = sum(values) if values and not incomplete and all(type(v) is int and v >= 0 for v in values) else None
    return {**totals, "source": "CLI reported parent turns; cached input is a subset of input; child usage is separate", "cost_usd": None,
            "auxiliary_calls": None, "routed_usage": subagents.routed_usage(directory / "requests.jsonl")}


def concise_result(value):
    if not value:
        return None
    if value["status"] != "completed":
        return copy.deepcopy(value)  # Preserve all evidence needed for a decision.
    result = copy.deepcopy(value)
    result["changed"] = [{"path": r["path"][:180], "summary": r["summary"][:160]} for r in value["changed"][:5]]
    result["details_in_artifacts"] = True
    result["details_truncated"] = result["changed"] != value["changed"]
    return result


class JobStore:
    def __init__(self, config, runs_root, projects):
        self.config = Path(config).resolve()
        self.root = Path(runs_root).resolve()
        self.projects = [Path(p).resolve() for p in projects]
        if not self.projects:
            fail("config_error", "Configure at least one allowed project root")
        self.children = []

    def directory(self, job_id):
        validate(job_id, JOB_ID)
        path = self.root / job_id
        if not path.resolve().is_relative_to(self.root) or not path.is_dir():
            fail("job_missing", "Unknown job")
        return path

    def launch(self, directory):
        self.children = [p for p in self.children if p.poll() is None]
        with (directory / "service.stderr.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, str(ROOT / "mcp_server.py"), "--execute-job", str(directory)],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                     start_new_session=os.name != "nt", cwd=ROOT)
        self.children.append(child)

    def open_panel(self, open_browser=False, user_request=None, project=None):
        from .panel_runtime import open_panel
        context = None
        if user_request is not None:
            validate(user_request, string(100000), "user_request")
            context = drafts.begin(self.root, self.projects, user_request, project)
        elif project is not None:
            fail("protocol_error", "project requires the current user_request")
        result = open_panel(self, open_browser)
        if context:
            result["task"] = context
            result["next"] = "Open the returned URL first, then continue planning in THIS turn: inspect only the facts needed for planning and call relay_prepare with this project, invocation_id and original user_request. Do not wait for another user message unless they explicitly asked to pause. Finish only after the plan is saved and ready for confirmation. Preparation must not implement the task, generate deliverables, edit task files, run implementation commands or call relay_submit. The user can add optional requirements in the panel and click Start after the plan arrives."
        return result

    def submit(self, project, task_key, plan, user_request=None, review_mode=None, subagents=None, draft_id=None):
        if draft_id is not None:
            draft = self.draft()
            if not draft or draft.get("id") != draft_id or not draft.get("plan") or draft.get("status", "ready") != "ready" or Path(project).resolve() != Path(draft["project"]).resolve():
                fail("draft_stale", "The displayed plan was replaced or preparation failed; refresh the current task")
        operation = {"action": "start", "plan": plan}
        if user_request is not None:
            validate(user_request, string(100000), "user_request")
            operation["request"] = user_request
        if review_mode is not None:
            operation["review_mode"] = review_mode
        if subagents is not None:
            operation["subagents"] = subagents
        return self._submit(project, task_key, operation)

    def prepare(self, project, plan, user_request=None, instructions=None, invocation_id=None):
        request = user_request or (plan.get("goal", "") if isinstance(plan, dict) else "")
        current = self.draft() or {}
        if invocation_id:
            validate(invocation_id, JOB_ID)
            if current.get("invocation_id") != invocation_id:
                fail("draft_stale", "This invocation was replaced by a newer task")
            if request.strip() != current.get("user_request", "").strip() or Path(project).resolve() != Path(current["project"]).resolve():
                fail("draft_stale", "Task or project changed; start a new task-bearing invocation")
        elif current.get("status") in ("awaiting_refinement", "preparing", "blocked") and current.get("invocation_id"):
            if request.strip() != current.get("user_request", "").strip():
                fail("draft_stale", "A different task is being prepared")
            invocation_id = current["invocation_id"]
        try:
            return self._prepare(project, plan, user_request, instructions, invocation_id)
        except (RelayError, OSError) as exc:
            if getattr(exc, "code", None) != "draft_stale":
                drafts.failure(self.root, project, request, exc, invocation_id)
            raise

    def _prepare(self, project, plan, user_request=None, instructions=None, invocation_id=None):
        project = Path(project).resolve()
        if not any(project == p or project.is_relative_to(p) for p in self.projects):
            fail("project_denied", "Project is outside the configured allowed roots")
        if self.root.is_relative_to(project):
            fail("unsafe_path", "Draft storage must live outside the project")
        validate(plan, HANDOFF, "handoff")
        plan = copy.deepcopy(plan)
        if user_request is not None:
            validate(user_request, string(100000), "user_request")
        if instructions is not None:
            validate(instructions, array(string()), "instructions")
            plan["constraints"] = list(dict.fromkeys(plan.get("constraints", []) + instructions))
        config = execution_config(self.config)
        expanded = expand_plan(plan, config["checks"])
        from .runner import code_fact_snapshot
        facts = code_fact_snapshot(project, expanded)
        value = {"project": str(project), "plan": copy.deepcopy(plan), "code_facts": facts}
        if invocation_id:
            value["invocation_id"] = invocation_id
            if (self.draft() or {}).get("generated_project"):
                value["generated_project"] = True
        if user_request is not None:
            value["user_request"] = user_request
        if instructions is not None:
            value["instructions"] = list(instructions)
        identity = hashlib.sha256(encoded(value).encode()).hexdigest()[:24]
        drafts.save(self.root, {**value, "id": identity, "status": "ready", "created_at": time.time()}, invocation_id)
        return {"draft_id": identity, "project": str(project), "goal": plan["goal"], "model_calls": 0,
                "next": "Review the current caller's plan in the panel; Start calls only the execution model."}

    def draft(self):
        return drafts.read(self.root)

    def submit_task(self, project, task_key, request, review_mode=None, subagents=None):
        if not isinstance(request, str) or not request.strip():
            fail("protocol_error", "Task request is required")
        operation = {"action": "plan_and_run", "request": request}
        if review_mode is not None:
            operation["review_mode"] = review_mode
        if subagents is not None:
            operation["subagents"] = subagents
        return self._submit(project, task_key, operation)

    def _submit(self, project, task_key, operation):
        if operation.get("review_mode") not in (None, "user_choice", "auto"):
            fail("protocol_error", "Unknown review mode")
        validate(task_key, {"type": "string", "pattern": "[A-Za-z0-9_-]{1,64}"})
        project = Path(project).resolve()
        if "subagents" in operation:
            operation["subagents"] = subagents.policy(operation["subagents"], project)
        if not any(project == p or project.is_relative_to(p) for p in self.projects):
            fail("project_denied", "Project is outside the configured allowed roots")
        if self.root.is_relative_to(project):
            fail("unsafe_path", "Plugin run storage must live outside the target repository")
        digest = hashlib.sha256(encoded({"project": os.path.normcase(str(project)), "task_key": task_key}).encode()).hexdigest()[:24]
        # Keep existing caller-plan idempotency keys compatible.
        payload = (operation["plan"] if operation["action"] == "start" and
                   not {"request", "review_mode", "subagents"}.intersection(operation) else operation)
        payload_hash = hashlib.sha256(encoded(payload).encode()).hexdigest()
        directory = self.root / digest
        self.root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.root / ".submit.lock"):
            if directory.exists():
                old = load_json(self.directory(digest) / "job.json")
                if old["payload_hash"] != payload_hash:
                    fail("task_key_conflict", "This task_key already has a different plan; resume it or choose a new task_key")
                return {"job_id": digest, "status": "existing", "reused": True}
            config = execution_config(self.config)
            if "subagents" in operation:
                config["subagents"] = operation["subagents"]
            subagents.policy(config.get("subagents"), project)
            if operation.get("review_mode") == "user_choice":
                config["review"] = False
            elif operation.get("review_mode") == "auto":
                config["review"] = True
            saved_operation = copy.deepcopy(operation)
            expanded = expand_plan(operation["plan"], config["checks"]) if operation["action"] == "start" else None
            if expanded is not None:
                saved_operation["plan"] = expanded
            # Validate facts and clean baseline before reserving an idempotency key.
            from .runner import code_fact_snapshot
            from .runtime import require_clean_project
            require_clean_project(project)
            if expanded is not None:
                code_fact_snapshot(project, expanded)
            directory.mkdir()
            atomic_json(directory / "config.json", config)
            atomic_json(directory / "job.json", {"project": str(project), "payload_hash": payload_hash,
                                                "title": operation.get("request") or expanded["goal"],
                                                "task_key": task_key, "created_at": time.time(),
                                                "policy_version": 1, "source_config": str(self.config)})
            atomic_json(directory / "operation.json", saved_operation)
            atomic_json(directory / "service.json", {"status": "queued", "time": time.time()})
            try:
                self.launch(directory)
            except OSError as exc:
                diagnostic = describe_failure(exc, {"stage": "启动接力后台进程", "executable": sys.executable},
                                              [directory / "service.stderr.log"])
                atomic_json(directory / "service.json", {"status": "stopped", "error": "launch_failed",
                                                         "diagnostic": diagnostic, "time": time.time()})
                fail("launch_failed", "Job was saved; use relay_resume after fixing local process startup")
        return {"job_id": digest, "status": "queued", "artifacts": str(directory / "relay"),
                "limits": {"worker_requests": config["worker"]["max_requests"],
                           "output_tokens_per_response": config["worker"].get("max_output_tokens"),
                           "request_bytes": config["limits"]["max_request_bytes"]}}

    def snapshot(self, job_id, full_result=False):
        directory = self.directory(job_id)
        run = directory / "relay"
        service = load_json(directory / "service.json")
        active = service["status"] in ("queued", "running") and time.time() - service["time"] < 15
        state = load_json(run / "state.json") if (run / "state.json").exists() else {}
        status = (state.get("status", service["status"]) if active or service["status"] == "finished"
                  else "interrupted" if service["status"] in ("queued", "running") else "stopped")
        row = {"job_id": job_id, "status": status, "active": active,
               "review_state": state.get("review_state"), "review_cycle": corrections.public_cycle(state),
               "correction_round": state.get("correction_round", 0)}
        if full_result:
            row["correction_history"] = corrections.history(run)
        children = []
        for phase in sorted(run.glob("[0-9]*-worker")):
            finished = load_json(phase / "finished.json") if (phase / "finished.json").is_file() else {}
            records = finished.get("subagents") or subagents.lifecycle(phase / "events.jsonl")
            children.extend({**agent, "phase": phase.name} for agent in records["agents"])
        if children:
            row["subagents"] = children if full_result else [{k: a[k] for k in ("id", "phase", "status", "closed") if k in a} for a in children]
        if not active:
            row.update(artifacts=str(run), result=copy.deepcopy(state.get("last_result")) if full_result else concise_result(state.get("last_result")),
                       checks=(copy.deepcopy(state.get("validation") or []) if full_result else
                               [{k: v[k] for k in ("id", "exit_code", "failure", "stdout_path", "stderr_path") if k in v}
                                for v in (state.get("validation") or [])]),
                       requests=load_json(run / "budget.json").get("roles", {}) if (run / "budget.json").exists() else {},
                       usage=usage_report(run))
            if state.get("last_result") and (run / "plan.json").is_file():
                row.update(review=read_review(run, state), review_revision=review_revision(run, state),
                           plan_source=state.get("plan_source", "api"))
                confirmation = read_confirmation(run, state)
                row["confirmation"] = confirmation
                if confirmation and not confirmation["stale"]:
                    row["status"] = "user_confirmed"
            if state.get("failure") or service.get("error"):
                row["failure"] = (state.get("failure") or {}).get("code", service.get("error"))
                row["diagnostic"] = state.get("failure") or service.get("diagnostic")
        revision = hashlib.sha256(encoded(row).encode()).hexdigest()[:16]
        return {**row, "revision": revision}

    def _confirmable(self, job_id, result_revision):
        row = self.snapshot(job_id)
        if row.get("active"):
            fail("busy", "Wait until execution finishes")
        run = self.directory(job_id) / "relay"
        state = load_json(run / "state.json")
        if (state.get("pending") or state.get("failure") or state.get("status") not in
                ("completed", "needs_manual_verification", "exhausted", "review_paused") or
                (state.get("last_result") or {}).get("status") != "completed"):
            fail("state_error", "There is no finished execution result to confirm")
        if review_revision(run, state) != result_revision:
            fail("review_stale", "Execution result changed; read it again")
        return run, state

    def confirm_result(self, job_id, result_revision):
        directory = self.directory(job_id)
        with FileLock(directory / "service.lock"):
            run, state = self._confirmable(job_id, result_revision)
            prior = read_confirmation(run, state)
            if prior and not prior["stale"]:
                return {"job_id": job_id, "status": "user_confirmed", "confirmation": prior, "model_calls": 0}
            value = {"source": "user", "time": time.time(), "result_revision": result_revision,
                     "summary": "用户已阅读执行结果，自行确认审核完毕并结束流程。",
                     "acknowledged_residual": list(range(len(state["last_result"].get("residual", []))))}
            atomic_json(run / "confirmation.json", value)
            if state.get("review_cycle"):
                state["review_cycle"].update(active=False, paused=False, reason="user_confirmed")
                state["review_state"] = "user_confirmed"
                corrections.update_round(run, state, status="user_confirmed")
                atomic_json(run / "state.json", state)
        return {"job_id": job_id, "status": "user_confirmed", "confirmation": value, "model_calls": 0}

    def request_review(self, job_id, result_revision, reviewer=None, auto_correct=True):
        if type(auto_correct) is not bool:
            fail("protocol_error", "auto_correct must be a boolean")
        directory = self.directory(job_id)
        with FileLock(directory / "service.lock"):
            run, state = self._confirmable(job_id, result_revision)
            if reviewer is None and state.get("plan_source") == "caller":
                fail("reviewer_mismatch", "Send the saved result back to its original caller conversation")
            prior = read_review(run, state)
            original = read_config(directory / "config.json")["planner"]
            target = reviewer or {"spec": original}
            prior_fingerprint = (prior or {}).get("reviewer_selection", {}).get("fingerprint")
            if prior and not prior_fingerprint and prior.get("reviewer") == original["model"]:
                prior_fingerprint = target_fingerprint(original)
            if prior and not prior["stale"] and prior_fingerprint == target_fingerprint(target["spec"]):
                return {"job_id": job_id, "status": "reviewed", "review": prior, "reused": True}
            confirmation = read_confirmation(run, state)
            if confirmation and not confirmation["stale"]:
                fail("state_error", "User already confirmed this result")
            (run / "correction-pause.json").unlink(missing_ok=True)
            atomic_json(run / "reviewer-target.json", {**target, "result_revision": result_revision})
            atomic_json(directory / "operation.json", {"action": "review", "result_revision": result_revision, "auto_correct": auto_correct})
            atomic_json(directory / "service.json", {"status": "queued", "time": time.time()})
        try:
            self.launch(directory)
        except OSError:
            atomic_json(directory / "service.json", {"status": "stopped", "time": time.time(), "error": "launch_failed"})
            raise
        return {"job_id": job_id, "status": "queued"}

    def record_review(self, job_id, result_revision, review, auto_correct=True):
        if type(auto_correct) is not bool:
            fail("protocol_error", "auto_correct must be a boolean")
        validate(review, schemas()["review"], "review")
        if auto_correct:
            validate_correction_review(review)
        directory = self.directory(job_id)
        with FileLock(directory / "service.lock"):
            if self.snapshot(job_id).get("active"):
                fail("busy", "Wait until execution finishes before reviewing")
            run = directory / "relay"
            state = load_json(run / "state.json")
            if (state.get("pending") or state.get("failure") or state.get("status") not in
                    ("completed", "needs_manual_verification", "exhausted", "review_paused") or
                    (state.get("last_result") or {}).get("status") != "completed"):
                fail("state_error", "Finish execution or resolve its failure before recording a final review")
            reviewer = "当前 Codex（原规划端）" if state.get("plan_source") == "caller" else "当前 Codex（用户指定审核）"
            confirmation = read_confirmation(run, state)
            if review.get("status") == "changes_requested" and confirmation and not confirmation["stale"]:
                fail("state_error", "User already confirmed this result")
            value = save_review(run, state, result_revision, review, reviewer, source="caller")
            state["review_cycle"] = {"kind": "caller", "auto_correct": auto_correct, "active": False}
            state["review_state"] = review["status"]
            corrections.update_round(run, state, status=review["status"], final_review=value)
            atomic_json(run / "state.json", state)
            if review["status"] == "changes_requested" and auto_correct:
                atomic_json(directory / "operation.json", {"action": "correct", "result_revision": result_revision})
                atomic_json(directory / "service.json", {"status": "queued", "time": time.time()})
        if review["status"] == "changes_requested" and auto_correct:
            self._launch_correction(directory)
            return {"job_id": job_id, "status": "queued", "review": value, "next": "Use relay_wait; read the new result/version and review it yourself in this conversation. Do not start another reviewer model or repeat task submission. Stop on approval, insufficient evidence, a blocker or a paused cycle.", "review_model_calls": 0, "execution_queued": True, "cost_usd": None}
        return {"job_id": job_id, "review": value, "model_calls": 0}

    def _launch_correction(self, directory):
        try:
            self.launch(directory)
        except OSError:
            atomic_json(directory / "service.json", {"status": "stopped", "error": "launch_failed", "time": time.time()})
            raise

    def correct_result(self, job_id, result_revision):
        directory = self.directory(job_id)
        with FileLock(directory / "service.lock"):
            run, state = self._confirmable(job_id, result_revision)
            confirmation = read_confirmation(run, state)
            if confirmation and not confirmation["stale"]:
                fail("state_error", "User already confirmed this result")
            review = read_review(run, state)
            if state["status"] != "review_paused" and (not review or review["stale"] or review["status"] != "changes_requested"):
                fail("state_error", "There is no rejected review to correct")
            atomic_json(directory / "operation.json", {"action": "correct", "result_revision": result_revision, "allow_retry": True})
            atomic_json(directory / "service.json", {"status": "queued", "time": time.time()})
        self._launch_correction(directory)
        return {"job_id": job_id, "status": "queued"}

    def pause_corrections(self, job_id):
        directory = self.directory(job_id)
        state = load_json(directory / "relay/state.json")
        if not state.get("review_cycle", {}).get("active"):
            fail("state_error", "There is no active review correction cycle")
        atomic_json(directory / "relay/correction-pause.json", {"requested_at": time.time()})
        return {"job_id": job_id, "status": "pause_requested", "model_calls": 0}

    def wait(self, job_id, seconds=20, after_revision=None, full_result=False):
        if type(seconds) is not int or not 0 <= seconds <= 50:
            fail("invalid_wait", "seconds must be an integer from 0 to 50")
        deadline = time.monotonic() + seconds
        while True:
            value = self.snapshot(job_id, full_result=full_result)
            if not value["active"] or time.monotonic() >= deadline:
                break
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        if value["revision"] == after_revision:
            return {"job_id": job_id, "status": value["status"], "revision": after_revision, "changed": False}
        return value

    def _upgrade_legacy_config(self, directory, state):
        """On explicit resume, remove v0.2 reductions using operator limits.

        Keep the execution endpoint/model/checks and cumulative ledger unchanged.
        No active process or historical completed run is rewritten.
        """
        job = load_json(directory / "job.json")
        if job.get("policy_version", 0) >= 1:
            return state
        source = execution_config(job.get("source_config", self.config))
        saved = read_config(directory / "config.json")
        if not (directory / "config-before-quality-fix.json").exists():
            atomic_json(directory / "config-before-quality-fix.json", saved)
        saved["worker"]["max_requests"] = source["worker"]["max_requests"]
        if "max_output_tokens" in source["worker"]:
            saved["worker"]["max_output_tokens"] = source["worker"]["max_output_tokens"]
        else:
            saved["worker"].pop("max_output_tokens", None)
        for key in ("max_request_bytes", "max_result_bytes", "max_fix_rounds", "max_replans"):
            saved["limits"][key] = source["limits"][key]
        atomic_json(directory / "config.json", saved)
        if (state and state["status"] == "exhausted" and state.get("plan_source") == "caller"
                and (state.get("last_result") or {}).get("status") == "completed"
                and state["fix_rounds"] < saved["limits"]["max_fix_rounds"]):
            state.update(status="ready", next="checks", failure=None)
            atomic_json(directory / "relay" / "state.json", state)
        job.update(policy_version=1, limits_source=str(self.config))
        atomic_json(directory / "job.json", job)
        return state

    def resume(self, job_id, plan=None, retry_interrupted=False):
        directory = self.directory(job_id)
        with FileLock(directory / "service.lock"):
            row = self.snapshot(job_id)
            if row.get("active"):
                fail("busy", "Job is already queued or running")
            run = directory / "relay"
            state = load_json(run / "state.json") if (run / "state.json").exists() else None
            if not state or state["status"] not in TERMINAL or state["status"] in ("awaiting_plan", "exhausted"):
                state = self._upgrade_legacy_config(directory, state)
            if state and state["status"] in TERMINAL and state["status"] != "awaiting_plan":
                return row
            action = "resume"
            expanded = None
            if plan is not None:
                if not state or state["status"] != "awaiting_plan":
                    fail("state_error", "Revised plans are accepted only for awaiting_plan")
                expanded = expand_plan(plan, read_config(directory / "config.json")["checks"])
                action = "replan"
            elif state and state["status"] == "awaiting_plan":
                fail("plan_required", "Return a concise revised plan addressing the named decision")
            elif not state:
                original = load_json(directory / "operation.json")
                action = original["action"]
                expanded = original.get("plan")
            operation = {"action": action, "plan": expanded, "retry_interrupted": retry_interrupted}
            if action == "plan_and_run":
                operation["request"] = original["request"]
            atomic_json(directory / "operation.json", operation)
            atomic_json(directory / "service.json", {"status": "queued", "time": time.time()})
        self.launch(directory)
        return {"job_id": job_id, "status": "queued"}


def execute_job(directory):
    directory = Path(directory).resolve()
    # A second process can never launch an overlapping worker for this job.
    with FileLock(directory / "service.lock"):
        stop = threading.Event()

        def heartbeat():
            while not stop.is_set():
                atomic_json(directory / "service.json", {"status": "running", "time": time.time()})
                stop.wait(2)

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        error = None
        diagnostic = None
        runner = None
        try:
            job, operation = load_json(directory / "job.json"), load_json(directory / "operation.json")
            runner = Runner(read_config(directory / "config.json"), job["project"], directory / "relay")
            with FileLock(directory / "relay.lock"):
                if operation["action"] == "start":
                    runner.start_with_plan(operation["plan"], operation.get("request"))
                elif operation["action"] == "plan_and_run":
                    runner.start(operation["request"])
                elif operation["action"] == "replan":
                    runner.resume_with_plan(operation["plan"])
                elif operation["action"] == "review":
                    runner.review_result(operation["result_revision"], operation.get("auto_correct", True))
                elif operation["action"] == "correct":
                    runner.correct_review(operation["result_revision"], operation.get("allow_retry", False))
                else:
                    runner.resume(operation.get("retry_interrupted", False))
        except (RelayError, OSError, ValueError) as exc:
            diagnostic = describe_failure(exc, runner.context if runner else {"stage": "读取任务配置", "path": str(directory)},
                                          [directory / "service.stderr.log", directory / "service.json"])
            error = diagnostic["code"]
        except Exception as exc:
            diagnostic = describe_failure(exc, runner.context if runner else {"stage": "任务初始化"},
                                          [directory / "service.stderr.log", directory / "service.json"])
            error = diagnostic["code"]
        finally:
            stop.set()
            thread.join(timeout=3)
            atomic_json(directory / "service.json", {"status": "stopped" if error else "finished",
                                                     "error": error, "diagnostic": diagnostic, "time": time.time()})

