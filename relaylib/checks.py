"""Explicit model checks. Never runs on page load, refresh or task start.

Each generation/compat check is one user-initiated Codex run in a disposable
read-only project. The gateway limit is 1 forwarded request, so a retry inside
the CLI is denied rather than silently charged twice. No automatic retries,
model switching or task-budget changes.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import __version__
from .catalog import Catalog
from .contracts import RelayError, fail, schemas, validate
from .diagnostics import describe_failure
from .gateway import Gateway, Ledger, healthcheck, resolve_endpoint
from .runtime import FileLock, atomic_json, git, project_snapshot, run_process
from .runner import parse_events

RECORDS = ".model-checks.json"
LOCK = ".model-check.lock"
KINDS = {
    "catalog": {"label": "查看模型列表", "costs": False,
                "proves": "网络、目录认证和模型是否出现在目录中；不代表模型能生成。"},
    "generation": {"label": "试着回答一句", "costs": True,
                   "proves": "所选模型当前能否通过真实 Codex 返回非空回答；会产生一次模型请求。"},
    "compat": {"label": "试跑接力格式", "costs": True,
               "proves": "真实 Codex → 网关 → 模型 → 本地 JSON 校验是否正常；使用临时只读样例。"},
}
GENERATION_PROMPT = ("You are a local connectivity check. Reply with exactly the word OK and nothing else. "
                     "Do not use tools, do not run commands, do not modify files, do not spawn subagents.")
COMPAT_HEADER = ("You are a local compatibility check. Return ONLY one JSON object, without Markdown fences "
                 "or commentary, matching this JSON Schema exactly:\n")


def signature(revision, endpoint, model, kind):
    payload = json.dumps({"revision": revision, "endpoint": endpoint, "model": model, "kind": kind,
                          "version": __version__}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def record_path(root):
    return Path(root) / RECORDS


def _load(root):
    path = record_path(root)
    if not path.is_file():
        return {"version": 1, "records": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "records": {}}
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("records"), dict):
        return {"version": 1, "records": {}}
    return value


def _endpoint_label(endpoint):
    return endpoint.get("base_url") or ("已有供应商 " + endpoint.get("codex_provider", ""))


def list_records(root, settings):
    """Records are invalid as soon as the endpoint, model, credential ref or config changes."""
    revision, config, profiles, _ = _context(settings)
    stored = _load(root)
    providers = {p["id"]: p for p in profiles}
    rows = []
    for key, record in stored["records"].items():
        if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
            continue
        profile = providers.get(record.get("provider_id"))
        current = (signature(revision, profile["endpoint"], record.get("model"), record["kind"])
                   if profile and isinstance(record.get("model"), str) else None)
        row = dict(record)
        row["valid"] = bool(current and record.get("signature") == current)
        row["invalid_reason"] = None if row["valid"] else "接口、模型或凭据配置已变化；此记录只说明当时的结果。"
        rows.append(row)
    rows.sort(key=lambda row: row.get("time") or 0, reverse=True)
    return {"version": __version__, "revision": revision, "kinds": KINDS, "records": rows,
            "model_calls": 0, "note": "历史通过只说明测试当时成功；打开页面和刷新不会运行任何检查。"}


def _context(settings):
    try:
        revision = settings.revision()
        config, profiles, _ = settings.read()
    except Exception as error:
        fail("config_error", getattr(error, "code", "config_error"))
    return revision, config, profiles, {p["id"]: p for p in profiles}


def run_check(root, settings, provider_id, model, kind):
    """Execute exactly one explicit check and persist its latest record."""
    if kind not in KINDS:
        fail("protocol_error", "Unknown check kind")
    revision, config, profiles, providers = _context(settings)
    profile = providers.get(provider_id)
    if profile is None:
        fail("config_error", "Selected provider is not saved")
    if not isinstance(model, str) or model not in profile["models"]:
        fail("config_error", "Select and save this model before checking it")
    started = time.monotonic()
    with FileLock(Path(root) / LOCK):
        try:
            if kind == "catalog":
                record = _catalog(profile, model)
            elif kind == "generation":
                record = _codex_check(profile, model, config, "generation", Path(root) / ".check-diagnostics")
            else:
                record = _codex_check(profile, model, config, "compat", Path(root) / ".check-diagnostics")
        except RelayError as error:
            record = _failure(kind, profile, model, error.code,
                              getattr(error, "upstream_status", None),
                              message=str(error) if isinstance(error, _CatalogFailure) else None,
                              elapsed=getattr(error, "elapsed", None),
                              stage=_failure_stage(error))
            record["model_calls"] = getattr(error, "model_calls", 0)
            if getattr(error, "logs", None):
                record["logs"] = error.logs
                record["action"] += " 本地诊断：" + "；".join(error.logs)
        except OSError as error:
            record = _failure(kind, profile, model,
                              "permission_denied" if isinstance(error, PermissionError) else "local_io_error",
                              None, stage="本地检查环境")
        record.update(provider_id=provider_id, provider_name=profile["name"], model=model,
                      endpoint=_endpoint_label(profile["endpoint"]), kind=kind,
                      time=time.time(), elapsed_seconds=round(time.monotonic() - started, 3),
                      signature=signature(revision, profile["endpoint"], model, kind),
                      version=__version__,
                      limit="generation and compat forward at most one model request")
        stored = _load(root)
        stored["records"][hashlib.sha256(f"{provider_id}|{model}|{kind}".encode()).hexdigest()[:24]] = record
        atomic_json(record_path(root), stored)
    return record


def _failure_stage(error):
    """Name the step that actually failed; never a generic 'model check'."""
    code = getattr(error, "code", None)
    status = getattr(error, "upstream_status", None)
    if isinstance(error, _CatalogFailure):
        return "读取模型目录"
    if code == "protocol_error":
        return "本地 JSON 校验"
    if code in ("cli_initialization_error",):
        return "初始化 Codex 本地执行环境"
    if code in ("process_or_api_error", "empty_model_answer"):
        return "运行模型进程"
    if code in ("request_limit",):
        return "本地请求门控"
    if code in ("timeout", "log_limit"):
        return "运行模型进程"
    if type(status) is int and status >= 300:
        return "上游模型请求"
    if code in ("check_modified_project",):
        return "复检临时样例"
    return "本地检查环境"


def _failure(kind, profile, model, code, status, message=None, elapsed=None, stage="模型检查"):
    diagnostic = describe_failure(RelayError(code, ""), {"stage": stage, "model": model,
                                                         "endpoint": _endpoint_label(profile["endpoint"]),
                                                         "upstream_status": status})
    return {"status": "failed", "stage": stage, "code": code,
            "http_status": status if type(status) is int else None,
            "elapsed_seconds": elapsed,
            "message": message or diagnostic.get("message", "检查未通过"),
            "action": diagnostic.get("action", ""),
            "answer": "", "model_calls": 0}


def _catalog(profile, model):
    try:
        value = Catalog().traceable({"endpoint": profile["endpoint"]})
    except Exception as error:
        # SettingsError carries a locally written, secret-free message.
        trace = getattr(error, "trace", {}) or {}
        message = str(error) if isinstance(error, ValueError) else "无法读取接口模型目录"
        raise _CatalogFailure(message, trace.get("status"), trace.get("elapsed_seconds"))
    models = value["models"]
    listed = model in models
    if not listed:
        return {"status": "failed", "stage": "模型目录", "code": "model_catalog_mismatch",
                "http_status": value.get("status"), "elapsed_seconds": value.get("elapsed_seconds"),
                "message": f"目录读取成功（HTTP {value.get('status')}），但未列出所选模型 {model}。",
                "action": "选择接口实际返回的模型 ID，或确认该接口的目录是否完整。",
                "answer": "", "model_calls": 0, "models": len(models)}
    return {"status": "passed", "stage": "模型目录", "code": None, "http_status": value.get("status"),
            "elapsed_seconds": value.get("elapsed_seconds"),
            "message": f"目录读取成功（HTTP {value.get('status')}），共 {len(models)} 个模型，包含所选模型。",
            "action": "目录成功不代表模型能生成；如需确认生成，请单独运行最小生成测试。",
            "answer": "", "model_calls": 0, "models": len(models)}


class _CatalogFailure(RelayError):
    def __init__(self, message, status, elapsed):
        super().__init__("catalog_error", message)
        self.upstream_status = status
        self.elapsed = elapsed


def _codex_check(profile, model, config, kind, diagnostics_root=None):
    endpoint = resolve_endpoint(profile["endpoint"])
    healthcheck(endpoint)
    temp = Path(tempfile.mkdtemp(prefix="stage-relay-check-"))
    project = temp / "sample"
    project.mkdir()
    (project / "README.md").write_text("Temporary read-only compatibility sample.\n", encoding="utf-8")
    gate = None
    ledger = None
    outcome = None
    try:
        git(project, "init", "-q")
        git(project, "add", "README.md")
        git(project, "-c", "user.name=Stage Relay check", "-c", "user.email=relay@example.invalid",
            "commit", "-qm", "check sample")
        before = project_snapshot(project)
        prompt = _prompt(kind)
        ledger = Ledger(temp / "budget.json")
        with Gateway(endpoint, model, "worker", 1, "model-check", ledger, temp / "requests.jsonl",
                     config["limits"]["max_request_bytes"], config["worker"]["timeout_seconds"]) as gate:
            env = os.environ.copy()
            env["STAGE_RELAY_TOKEN"] = gate.token
            native = endpoint.get("native_auth", False)
            answer = temp / "answer.txt"
            argv = list(config["codex_command"]) + [
                "exec", "-C", str(project), "-s", "read-only", "-m", model, "--ephemeral", "--json",
                "-o", str(answer), "-c", 'model_provider="stage_relay"',
                "-c", 'model_providers.stage_relay.name="Stage relay request gate"',
                "-c", "model_providers.stage_relay.base_url=" + json.dumps(gate.base_url),
                "-c", 'model_providers.stage_relay.wire_api="responses"',
                "-c", "model_providers.stage_relay.requires_openai_auth=" + str(native).lower(),
                "-c", "model_providers.stage_relay.supports_websockets=false",
                "-c", "model_providers.stage_relay.request_max_retries=0",
                "-c", "model_providers.stage_relay.stream_max_retries=0", "-"]
            if not native:
                argv[-1:-1] = ["-c", 'model_providers.stage_relay.env_key="STAGE_RELAY_TOKEN"']
            outcome = run_process(argv, project, prompt, config["worker"]["timeout_seconds"],
                                  config["limits"]["max_log_bytes"], temp / "events.jsonl",
                                  temp / "stderr.log", env=env, cancel=gate.cancel)
        status = gate.upstream_status
        if gate.failure:
            raise _GatewayFailure(gate.failure, status)
        if outcome.get("failure"):
            raise _GatewayFailure(outcome["failure"], status)
        if outcome["exit_code"] != 0:
            stderr = (temp / "stderr.log").read_text(encoding="utf-8", errors="replace")
            code = "cli_initialization_error" if "failed to initialize in-process app-server client" in stderr else "process_or_api_error"
            raise _GatewayFailure(code, status)
        if parse_events(temp / "events.jsonl")["event_errors"]:
            raise _GatewayFailure("process_or_api_error", status)
        if project_snapshot(project) != before:
            raise _GatewayFailure("check_modified_project", status)
        text = answer.read_text(encoding="utf-8", errors="replace").strip() if answer.is_file() else ""
        if not text:
            raise _GatewayFailure("empty_model_answer", status)
        if kind == "generation":
            return {"status": "passed", "stage": "生成回答", "code": None, "http_status": status,
                    "message": "模型返回了非空回答。", "action": "只证明当前可生成；任务效果和工具能力仍需自行验证。",
                    "answer": text[:200], "model_calls": 1}
        try:
            value = json.loads(text)
            validate(value, schemas()["result"], "result")
        except RelayError:
            raise _GatewayFailure("protocol_error", status)
        except (ValueError, TypeError):
            raise _GatewayFailure("protocol_error", status)
        return {"status": "passed", "stage": "本地 JSON 校验", "code": None, "http_status": status,
                "message": f"真实 Codex 请求经网关返回了符合结果 Schema 的 JSON（status={value.get('status')}）。",
                "action": "兼容性通过只说明本次链路可用；不代表任务效果、工具调用或后续请求一定成功。",
                "answer": "", "model_calls": 1}
    except RelayError as error:
        error.model_calls = ledger.value["roles"].get("worker", {}).get("forwarded", 0) if ledger else 0
        if diagnostics_root is not None:
            directory = Path(diagnostics_root) / uuid.uuid4().hex
            directory.mkdir(parents=True)
            # Keep only diagnostics, never argv, environment or HTTP request bodies.
            secrets = [gate.token] if gate else []
            for value in endpoint["headers"].values():
                if isinstance(value, str) and len(value) >= 6:
                    secrets.extend([value, value.removeprefix("Bearer ")])
            saved = []
            for name in ("stderr.log", "events.jsonl", "requests.jsonl", "answer.txt"):
                source = temp / name
                if not source.is_file() or source.is_symlink():
                    continue
                content = source.read_text(encoding="utf-8", errors="replace")
                for secret in sorted(set(secrets), key=len, reverse=True):
                    if secret:
                        content = content.replace(secret, "[REDACTED]")
                target = directory / name
                target.write_text(content, encoding="utf-8")
                saved.append(str(target))
            atomic_json(directory / "outcome.json", {"code": error.code, "process": outcome,
                                                       "model_calls": error.model_calls,
                                                       "http_status": getattr(error, "upstream_status", None)})
            error.logs = saved + [str(directory / "outcome.json")]
        raise
    finally:
        # Git objects are read-only on Windows; retry them with write access.
        def _writable(func, name, exc):
            checked = Path(name).resolve()
            if not checked.is_relative_to(temp.resolve()):
                raise exc
            os.chmod(name, 0o666)
            func(name)
        shutil.rmtree(temp, onexc=_writable) if sys.version_info >= (3, 12) else shutil.rmtree(temp, onerror=_writable)


class _GatewayFailure(RelayError):
    def __init__(self, code, status):
        super().__init__(code, "")
        self.upstream_status = status


def _prompt(kind):
    if kind == "generation":
        return GENERATION_PROMPT
    schema = json.dumps(schemas()["result"], ensure_ascii=False, separators=(",", ":"))
    return (COMPAT_HEADER + schema + "\n\nUse status \"completed\", empty changed/checks arrays, "
            "residual [\"compatibility check\"], evidence [\"compatibility check\"], and empty strings "
            "for failedStep, decisionNeeded, reason and blockedReason. Do not use tools or modify files.\n")
