"""Launch an existing, known local application; never launch a new model runner."""

import os
import json
import shutil
import subprocess
from pathlib import Path

from .domain import Invalid


def registered_openai_packages():
    """Explorer does not inherit Codex's private PATH; ask the user Appx registry."""
    if os.name!='nt':return []
    powershell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    script="@(Get-AppxPackage -Name 'OpenAI.*' | Sort-Object Version -Descending | ForEach-Object { $_.InstallLocation }) | ConvertTo-Json -Compress"
    try:
        result=subprocess.run([str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=8,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode or not result.stdout.strip():return []
        roots=json.loads(result.stdout)
        return [Path(p) for p in ([roots] if isinstance(roots,str) else roots) if isinstance(p,str) and Path(p).is_absolute()]
    except (OSError,ValueError,subprocess.TimeoutExpired):return []


def codex_executable():
    # Prefer the actual ChatGPT/Codex desktop package, including when an npm
    # command shim shadows it or the GUI has no CLI-specific PATH at all.
    for root in registered_openai_packages():
        executable=root/'app/resources/codex.exe'
        if (root/'AppxManifest.xml').is_file() and executable.is_file():
            return str(executable)
    for name in ('codex.exe','codex'):
        candidate=shutil.which(name)
        if candidate and Path(candidate).suffix.lower()=='.exe' and Path(candidate).is_file():
            return candidate
    raise Invalid('未能定位 ChatGPT / Codex 内置的原生程序。请重新检测安装位置；不需要另装一个 Codex。')


def dsh_runtime():
    target=application_target('dsh')
    if not target:
        raise Invalid('未定位到 DSH 桌面应用，请先安装或建立桌面快捷方式。')
    if target.lower().endswith('.lnk'):
        literal="'"+target.replace("'","''")+"'"
        script="(New-Object -ComObject WScript.Shell).CreateShortcut("+literal+").TargetPath"
        completed=subprocess.run([str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'),'-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,timeout=8,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        target=completed.stdout.strip()
    root=Path(target).parent/'resources/app/dsh'
    if not (root/'node_modules/@deepseek-ai/dsh-agent-loop/lib/index.js').is_file():
        raise Invalid('未找到兼容的 DSH 桌面运行时，未写入接入配置。')
    return root


def application_target(app, home=None):
    home = Path(home or Path.home())
    if app == "codex":
        package = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Packages/OpenAI.Codex_2p2nqsd0c76g0"
        if package.is_dir():
            return "shell:AppsFolder\\OpenAI.Codex_2p2nqsd0c76g0!App"
    if app == "antigravity":
        exe = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "Programs/antigravity/Antigravity.exe"
        if exe.is_file():
            return str(exe)
    labels = {"codex": ["Codex", "ChatGPT"], "dsh": ["DeepSeek Harness"], "antigravity": ["Antigravity"]}.get(app, [])
    roots = [home / "Desktop", Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs", Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs"]
    for root in roots:
        for label in labels:
            for target in [root / f"{label}.lnk", root / label / f"{label}.lnk"]:
                if target.is_file():
                    return str(target)
    return None


def dsh_launch(home=None):
    """How the desktop DSH is started, and why it cannot open a browser itself.

    Read from the installed desktop host: it boots profile `desktop` with
    `args: ["--no-open", "--host", "127.0.0.1", "--port", "0"]`. `--no-open` is
    built into the host, so a desktop launch does not hand the session to a
    browser. A browser tab therefore comes from somewhere else — a separate
    `dsh web` invocation — and adding the flag again here would only mislead.
    """
    home = Path(home or Path.home())
    target = application_target("dsh", home=home)
    return {"command": str(target) if target else None, "args": [],
            "surface": "desktop-host" if target else None,
            "no_open": True,
            "note": "桌面宿主启动时已带 --no-open；若仍出现浏览器页面，来源不是桌面启动链。"}


def open_application(app, test_home=None):
    if test_home is not None:
        return {"opened": False, "notice": "隔离测试不启动真实应用。"}
    if os.name != "nt":
        raise Invalid("当前自动打开功能适用于 Windows。")
    target = application_target(app)
    if not target:
        raise Invalid("暂未定位到此应用，请先从桌面启动一次；接入配置已保留。")
    os.startfile(target)
    if app == "dsh":
        plan = dsh_launch()
        return {"opened": True, "no_open": plan["no_open"],
                "notice": "已打开 DSH 桌面端。桌面宿主自带 --no-open，本身不会再开浏览器；"
                          "若另有浏览器页面，来源是独立启动的 dsh web，而不是这次启动。"}
    return {"opened": True, "notice": "已请求打开本机应用；任务仍需应用会话确认领取。"}


def open_application(app, test_home=None):
    if test_home is not None:
        return {"opened": False, "notice": "隔离测试不启动真实应用。"}
    if os.name != "nt":
        raise Invalid("当前自动打开功能适用于 Windows。")
    target = application_target(app)
    if not target:
        raise Invalid("暂未定位到此应用，请先从桌面启动一次；接入配置已保留。")
    if app == "dsh":
        plan = dsh_launch()
        if plan["no_open"]:
            subprocess.Popen([plan["command"], *plan["args"]])
            return {"opened": True, "no_open": True,
                    "notice": "已用 --no-open 启动 DSH 桌面宿主：只开桌面窗口，不再额外弹出浏览器页面。"}
        os.startfile(target)
        return {"opened": True, "no_open": False,
                "notice": "已通过快捷方式打开 DSH。快捷方式无法携带参数，若它同时弹出浏览器页面，"
                          "那是 DSH 的 openBrowser 默认行为；用 --no-open 启动可关闭。"}
    os.startfile(target)
    return {"opened": True, "notice": "已请求打开本机应用；任务仍需应用会话确认领取。"}
