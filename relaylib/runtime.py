"""Atomic checkpoints, process-tree deadlines, project fingerprints, and locks."""

import hashlib
from contextlib import ExitStack
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .contracts import fail, RelayError


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        fail("unsafe_path", f"Checkpoint is a symlink: {path.name}")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
        # Observers must not turn a successful stage into a recovery failure.
        for attempt in range(8):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 7:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        if temp.exists():
            temp.unlink()


class FileLock:
    """OS-owned lock: automatically released on crash; never steals a stale PID."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            fail("unsafe_path", "Lock must not be a symlink")
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            fail("busy", "Another relay process owns this project/run")
        return self

    def __exit__(self, *_):
        if self.stream:
            self.stream.close()


class WindowsJob:
    """Documented Windows Job Object: terminate descendants even if parent exits."""
    def __init__(self, proc):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(key, ctypes.c_ulonglong) for key in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(proc._handle))):
            code = ctypes.get_last_error()
            self.close()
            proc.kill()
            proc.wait(timeout=5)
            raise OSError(code, "Unable to establish a Windows process-tree job")

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def stop_tree(proc, job=None):
    if job and job.handle:
        job.terminate()
    elif proc.poll() is not None:
        return
    elif os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_process(argv, cwd, stdin_text, timeout, max_bytes, stdout_path, stderr_path,
                env=None, cancel=None):
    """Never invoke a shell; drain both streams while enforcing a combined disk cap."""
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    start = time.monotonic()
    outputs = ExitStack()
    try:
        stdout_file = outputs.enter_context(open(stdout_path, "wb"))
        stderr_file = outputs.enter_context(open(stderr_path, "wb"))
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, **flags)
        job = WindowsJob(proc)
    except BaseException:
        outputs.close()
        raise
    seen, mutex, overflow, io_failed = [0], threading.Lock(), threading.Event(), threading.Event()

    def drain(source, target):
        try:
            read_chunk = getattr(source, "read1", source.read)
            while chunk := read_chunk(4096):
                with mutex:
                    remaining = max(0, max_bytes - seen[0])
                    seen[0] += len(chunk)
                    if seen[0] > max_bytes:
                        overflow.set()
                target.write(chunk[:remaining])
                target.flush()  # Local observers can see events before phase exit.
        except OSError:
            io_failed.set()

    def feed():
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    readers = [threading.Thread(target=drain, args=(proc.stdout, stdout_file), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, stderr_file), daemon=True)]
    for t in readers:
        t.start()
    threading.Thread(target=feed, daemon=True).start()
    reason = None
    try:
        while proc.poll() is None:
            if cancel and cancel.is_set():
                reason = "request_limit"
            elif io_failed.is_set():
                reason = "log_io_error"
            elif overflow.is_set():
                reason = "output_limit"
            elif time.monotonic() - start > timeout:
                reason = "timeout"
            if reason:
                stop_tree(proc, job)
                break
            time.sleep(0.03)
    except BaseException:
        stop_tree(proc, job)
        raise
    finally:
        # Closing the job also kills background descendants of an already-exited parent.
        job.close()
        for t in readers:
            t.join(timeout=3)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        outputs.close()
    if overflow.is_set():
        reason = "output_limit"
    if io_failed.is_set():
        reason = "log_io_error"
    return {"exit_code": proc.returncode, "failure": reason,
            "elapsed_seconds": round(time.monotonic() - start, 3)}


def git(project, *args):
    try:
        run = subprocess.run(["git", "-C", str(project), *args], capture_output=True, timeout=30)
    except FileNotFoundError:
        fail("git_missing", "Git is not available to this process")
    except subprocess.TimeoutExpired:
        fail("git_timeout", "Git project check timed out")
    if run.returncode:
        fail("git_required", "Project needs an accessible Git repository with a commit")
    return run.stdout


def project_snapshot(project):
    head = git(project, "rev-parse", "HEAD").decode().strip()
    names = git(project, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(names)):
        if not name:
            continue
        p = Path(project) / os.fsdecode(name)
        digest.update(name + b"\0")
        if p.is_symlink():
            digest.update(os.readlink(p).encode("utf-8", errors="surrogateescape"))
        elif p.is_file():
            with p.open("rb") as stream:
                while block := stream.read(128 * 1024):
                    digest.update(block)
        else:
            digest.update(b"<deleted-or-directory>")
    return {"head": head, "files_sha256": digest.hexdigest()}


def require_clean_project(project):
    if not Path(project).is_dir():
        fail("project_missing", "Project directory is missing or is not a directory")
    root = Path(git(project, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if root != Path(project).resolve():
        fail("project_root", "Use the repository root as --project")
    try:
        git(project, "rev-parse", "--verify", "HEAD")
    except RelayError as error:
        if error.code == "git_required":
            fail("git_no_commit", "Project needs an initial commit")
        raise
    if git(project, "status", "--porcelain", "--untracked-files=all").strip():
        fail("dirty_project", "Commit/stash existing changes first, or use a clean checkout")
    return project_snapshot(project)


def bounded_read(path, limit=200_000):
    path = Path(path)
    if path.stat().st_size > limit:
        fail("input_limit", f"Input too large: {path.name}")
    return path.read_text(encoding="utf-8-sig")
