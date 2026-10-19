"""One reusable local GUI per pinned operator configuration; no model calls."""
import http.client
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import fail, load_json
from . import __version__
from .runner import ROOT
from .runtime import FileLock

_children = []  # Keep handles to reap exited launchers on subsequent opens.
HEALTH_TIMEOUT = 0.75  # A loopback socket either answers quickly or is gone.
START_TIMEOUT = 10.0
RECORD = ".panel.json"
LAUNCH_LOG = ".panel-runtime.json"


def _identity(jobs):
    return {"config": str(jobs.config), "projects": [str(p) for p in jobs.projects], "version": __version__}


MAX_TIMINGS = 400


def _timed(marker, phase, started):
    """Append one bounded local timing row; opening is never a model call."""
    row = {"phase": phase, "ms": round((time.monotonic() - started) * 1000, 1), "time": time.time()}
    marker = Path(marker)
    try:
        lines = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
        lines.append(json.dumps(row, ensure_ascii=False))
        marker.write_text("\n".join(lines[-MAX_TIMINGS:]) + "\n", encoding="utf-8")
    except OSError:
        pass
    return row


def _health(url, instance):
    """Return the URL only when this exact panel instance answers.

    A loopback health read is a direct HTTP/1.1 request: urllib builds a proxy
    opener per call (about 20 ms here), which is pure overhead on every open.
    """
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            return None
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=HEALTH_TIMEOUT)
        try:
            connection.request("GET", parsed.path + "api/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                return None
            value = json.loads(response.read(65_536))
        finally:
            connection.close()
        return url if isinstance(value, dict) and value.get("instance") == instance else None
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return None


def _recorded(jobs):
    """Read the control file; missing or unreadable records are not errors."""
    try:
        value = load_json(jobs.root / RECORD, 65_536)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _reusable(jobs, timings=None, mismatch=None):
    """Reuse a healthy panel for the same config and projects.

    A different installed version is reused too, never started a second time:
    the caller reports the mismatch instead of letting two versions race. When
    `mismatch` is a dict it receives the recorded version for that notice.
    """
    started = time.monotonic()
    value = _recorded(jobs)
    if timings is not None:
        timings.append(_timed(jobs.root / LAUNCH_LOG, "read_record", started))
    if not value:
        return None
    identity = value.get("identity") or {}
    current = _identity(jobs)
    if {k: identity.get(k) for k in ("config", "projects")} != {k: current[k] for k in ("config", "projects")}:
        return None
    try:
        parsed = urlsplit(value["url"])
    except (KeyError, TypeError, ValueError):
        return None
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not value.get("instance"):
        return None
    started = time.monotonic()
    url = _health(value["url"], value["instance"])
    if timings is not None:
        timings.append(_timed(jobs.root / LAUNCH_LOG, "health", started))
    if url and mismatch is not None and identity.get("version") != current["version"]:
        mismatch.update(panel_version=identity.get("version"), plugin_version=current["version"])
    return url


HISTORY = ".panel-history.jsonl"
MAX_HISTORY = 20
MAX_STALE_PROBES = 3


def _remember(jobs, value):
    """Append bounded local history so other-version panels can be detected."""
    row = {"version": value.get("identity", {}).get("version"), "url": value.get("url"),
           "pid": value.get("pid"), "instance": value.get("instance"), "time": time.time()}
    path = jobs.root / HISTORY
    try:
        rows = path.read_text(encoding="utf-8").splitlines()[-MAX_HISTORY:] if path.exists() else []
        if rows:
            try:
                if json.loads(rows[-1]).get("instance") == row["instance"]:
                    return  # Repeated reuse of the same panel is already recorded.
            except ValueError:
                pass
        rows.append(json.dumps(row, ensure_ascii=False))
        path.write_text("\n".join(rows[-MAX_HISTORY:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _stale_panels(jobs):
    """Healthy panels from another installed version; probes are bounded and local."""
    current = _recorded(jobs) or {}
    current_instance = current.get("instance")
    if current.get("identity", {}).get("version") != __version__:
        return []
    rows, seen = [], {current_instance}
    try:
        lines = (jobs.root / HISTORY).read_text(encoding="utf-8").splitlines()[-MAX_HISTORY:]
    except OSError:
        return rows
    for line in reversed(lines):
        if len(rows) >= MAX_STALE_PROBES:
            break
        try:
            value = json.loads(line)
        except ValueError:
            continue
        instance, url = value.get("instance"), value.get("url", "")
        if not instance or instance in seen or not url:
            continue
        seen.add(instance)
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            continue
        if value.get("version") == __version__:
            continue
        if _health(url, instance):
            rows.append({"version": value.get("version", "未知"), "pid": value.get("pid"), "url": url})
    return rows


def _shortcut_path(jobs):
    return jobs.root / ("打开面板.cmd" if os.name == "nt" else "open-panel.sh")


SHORTCUT = """@echo off
rem Stage Relay: opens or reuses the local panel. No model requests.
setlocal
"{python}" "{server}" --config "{config}" --runs-root "{runs}"{projects} --open-browser --open-panel
if errorlevel 1 pause
"""


def ensure_shortcut(jobs):
    """Create the no-model shortcut once; never register autostart."""
    path = _shortcut_path(jobs)
    projects = "".join(' --projects-root "%s"' % project for project in jobs.projects)
    text = SHORTCUT.format(python=sys.executable, server=ROOT / "mcp_server.py", config=jobs.config,
                           runs=jobs.root, projects=projects)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            previous = path.read_text(encoding="ascii")
            if previous == text or "rem Stage Relay: opens or reuses the local panel." not in previous:
                return str(path)
        # ASCII-only batch content; the file name itself may be localized.
        with path.open("w", encoding="ascii", newline="\r\n") as stream:
            stream.write(text)
    except OSError:
        return None
    return str(path)


def launch_hint(jobs):
    """Where the local, model-free shortcut lives, if it could be written."""
    path = _shortcut_path(jobs)
    return {"path": str(path), "model_calls": 0,
            "notice": "双击即可打开或复用面板；不会注册开机自启。"} if path.is_file() else None


def open_panel(jobs, open_browser=False):
    started_all = time.monotonic()
    _children[:] = [p for p in _children if p.poll() is None]
    jobs.root.mkdir(parents=True, exist_ok=True)
    record = jobs.root / RECORD
    timings = []

    started = time.monotonic()
    mismatch = {}
    with FileLock(jobs.root / ".panel-launch.lock"):
        url = _reusable(jobs, timings, mismatch)
        reused = url is not None
        if not url:
            command = [sys.executable, str(ROOT / "panel.py"), "--config", str(jobs.config),
                       "--runs-root", str(jobs.root), "--port", "0", "--control-file", str(record)]
            for project in jobs.projects:
                command.extend(["--projects-root", str(project)])
            started = time.monotonic()
            with (jobs.root / ".panel.stderr.log").open("ab") as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                         cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                         start_new_session=os.name != "nt")
            _children.append(child)
            timings.append(_timed(jobs.root / LAUNCH_LOG, "spawn", started))
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline and child.poll() is None:
                url = _reusable(jobs)
                if url:
                    break
                time.sleep(0.05)
            if not url:
                if child.poll() is None:
                    child.terminate()
                fail("panel_start_failed", "Local panel did not become ready; inspect .panel.stderr.log")
            timings.append(_timed(jobs.root / LAUNCH_LOG, "ready", started))
        recorded = _recorded(jobs)
        if recorded:
            _remember(jobs, recorded)
    timings.append(_timed(jobs.root / LAUNCH_LOG, "total", started_all))
    opened = bool(webbrowser.open(url)) if open_browser else False
    stale = _stale_panels(jobs)
    result = {"url": url, "reused": reused, "browser_opened": opened, "model_calls": 0,
              "version": __version__, "timings": timings,
              "shortcut": ensure_shortcut(jobs),
              "next": "Open this URL in the client's browser panel. Models start only when the user submits a task."}
    if mismatch:
        result["reconnect_required"] = True
        result["version_mismatch"] = mismatch
        result["reconnect_notice"] = (
            "现有面板版本 " + str(mismatch.get("panel_version")) + " 与当前插件版本 " +
            str(mismatch.get("plugin_version")) + " 不一致。已复用现有面板以避免重复启动服务；"
            "请在本客户端重新连接 Stage Relay 插件，再关闭旧面板后重新打开，以加载当前版本界面。")
    elif stale:
        result["reconnect_required"] = True
        result["reconnect_notice"] = (
            "检测到旧版本面板进程 " + "、".join(str(row["version"]) for row in stale) +
            " 仍在运行。请在本客户端重新连接 Stage Relay 插件（旧会话不会自动加载新版），"
            "再打开面板；不要同时保留多个面板地址，以免新旧版本反复启动服务。")
        result["stale_panels"] = stale
    return result



