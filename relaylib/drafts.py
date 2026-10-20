"""Task-bearing invocations replace stale plans before planning starts. Local only."""
import time
import uuid
from pathlib import Path

from .contracts import RelayError, fail, load_json
from .diagnostics import describe_failure
from .runtime import FileLock, atomic_json, git


def read(root):
    path = Path(root) / ".caller-plan.json"
    return load_json(path, 2_000_000) if path.is_file() else None


def save(root, value, invocation_id=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / ".draft.lock"):
        current = read(root)
        if invocation_id and (current or {}).get("invocation_id") != invocation_id:
            fail("draft_stale", "Another invocation replaced this task; do not restore an older plan")
        atomic_json(root / ".caller-plan.json", value)


def failure(root, project, request, exc, invocation_id=None):
    value = {"id": uuid.uuid4().hex[:24], "status": "blocked", "plan": None,
             "user_request": request, "project": str(project), "created_at": time.time(),
             "error": describe_failure(exc, {"stage": "准备当前任务", "path": str(project)})}
    if invocation_id:
        value["invocation_id"] = invocation_id
    if getattr(exc, "code", None) == "protocol_error":
        value["error"].update(message="当前方案尚未通过本地校验。", action="方案中的现有文件引用必须真实存在；新建文件请写入实施步骤，用 README.md 等现有文件作为事实锚点。核对项目路径与方案字段后重新准备。")
    save(root, value, invocation_id)
    return value


def begin(root, projects, request, project=None):
    identity = uuid.uuid4().hex[:24]
    generated = project is None
    root = Path(root).resolve()
    if generated:
        base = root.parent / "projects"
        if not any(base.is_relative_to(p) for p in projects):
            base = projects[0] / "stage-relay-projects"
        project = base / identity
    project = Path(project).resolve()
    value = {"id": identity, "invocation_id": identity, "status": "preparing", "plan": None,
             "project": str(project), "user_request": request, "generated_project": generated,
             "created_at": time.time()}
    save(root, value)
    try:
        if not any(project == p or project.is_relative_to(p) for p in projects):
            fail("project_denied", "Project is outside allowed roots")
        if root.is_relative_to(project):
            fail("unsafe_path", "Run storage must be outside the execution project")
        if generated:
            project.mkdir(parents=True, exist_ok=False)
            (project / "README.md").write_text("# Stage Relay task\n\n" + request + "\n", encoding="utf-8")
            git(project, "init", "-q")
            git(project, "add", "--", "README.md")
            git(project, "-c", "user.name=Stage Relay", "-c", "user.email=stage-relay@local.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "Initialize task workspace")
        elif not project.is_dir():
            fail("project_missing", "Project directory is missing")
    except (RelayError, OSError) as exc:
        return failure(root, project, request, exc, identity)
    return value
