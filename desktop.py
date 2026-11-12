"""Native Windows application entry point, packaged with WebView2 and Python."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path
from urllib.request import urlopen

from relay.domain import Store
from relay.httpd import PanelServer
from relay import __version__
from relay.storage import application_dir, default_data_dir


class DesktopAPI:
    def __init__(self):
        self._window = None

    def choose_workspace(self):
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER, allow_multiple=False)
        return str(Path(chosen[0]).resolve()) if chosen else None


def decide_occupied_port(health, port, version, data_dir):
    """What to do when the port is already held, given the holder's health.

    Returns None when this launch should attach to the running instance, or the
    message explaining why it cannot. Kept separate from main() so the decision can
    be checked without a window, a mutex, or a file handle — the three things that
    made this hard to test in place.

    Attaching is the normal case: double-clicking the app while it is running must
    open the window that is already serving, not refuse. A refusal is correct only
    when the other instance is a different build or serves a different store,
    because then this process could not honour either.
    """
    running_dir = health.get("data_dir")
    same_dir = bool(running_dir) and Path(running_dir).resolve() == Path(data_dir).resolve()
    same_version = health.get("version") == version
    is_us = health.get("app") == "stage-relay"

    # "Attach" is only ever right for our own app. A different program that happens
    # to report the same version and directory must not be opened as if it were us.
    if is_us and same_version and same_dir:
        return None

    if not is_us:
        return (f'端口 {port} 被其他应用占用（app={health.get("app")!r}）。\n\n'
                f'本次版本：{version}')

    return (
        f'端口 {port} 已被另一个 Stage Relay 实例占用。\n\n'
        f'正在运行：\n'
        f'  版本     {health.get("version")}\n'
        f'  PID      {health.get("pid")}\n'
        f'  端口     {port}\n'
        f'  数据目录 {running_dir}\n\n'
        f'本次启动：\n'
        f'  版本     {version}\n'
        f'  端口     {port}\n'
        f'  数据目录 {data_dir}\n\n'
        + ('两个实例的数据目录不同，同一个端口只能服务一份数据。\n'
           if not same_dir else
           '正在运行的是另一个版本，本次无法接管它的端口。\n')
        + '请先关闭上面列出的那个实例再启动本次版本；任务和文件都会保留。\n'
          '本应用不会自动结束它。')


def main():
    if '--codex-native-worker' in sys.argv:
        from relay.codex_worker import main as native_worker
        native_worker(sys.argv[sys.argv.index('--codex-native-worker')+1:])
        return
    parser = argparse.ArgumentParser(description="Stage Relay desktop")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--app-home", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.data_dir = (args.data_dir or default_data_dir()).resolve()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    # Windowed executables have no console streams. Keep diagnostics local.
    log_stream = (args.data_dir / "desktop.log").open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log_stream
    if sys.stderr is None:
        sys.stderr = log_stream
    logging.basicConfig(filename=args.data_dir / "desktop.log",level=logging.WARNING,format="%(asctime)s %(levelname)s %(message)s",encoding="utf-8")
    mutex = None
    server = None
    try:
        if os.name == "nt":
            kernel = ctypes.windll.kernel32
            kernel.CreateMutexW.restype = ctypes.c_void_p
            mutex = kernel.CreateMutexW(None, False, f"Local\\StageRelayDesktop-{args.port}")
            if kernel.GetLastError() == 183:
                try:
                    with urlopen(f'http://127.0.0.1:{args.port}/api/health',timeout=3) as response:
                        health=json.load(response)
                        version=health.get('version')
                        running_dir=health.get('data_dir')
                except (OSError,ValueError):
                    version=None
                    running_dir=None
                if running_dir and Path(running_dir).resolve()!=args.data_dir:
                    ctypes.windll.user32.MessageBoxW(None,f'端口 {args.port} 上的 Stage Relay 加载的数据目录与本次不同。\n\n正在运行：\n{running_dir}\n\n本次请求：\n{args.data_dir}\n\n继续打开只会显示正在运行那个实例的数据。要换目录请先用 --data-dir 指定同一路径。','Stage Relay',0x40)
                if version != __version__:
                    ctypes.windll.user32.MessageBoxW(None,
                        f'已有另一个版本的 Stage Relay 正在运行。\n\n'
                        f'正在运行：版本 {version}，端口 {args.port}，数据目录 {running_dir}\n'
                        f'本次打开：版本 {__version__}\n\n'
                        f'请先关闭旧版窗口，再打开新版。旧版任务和文件会保留；'
                        f'本应用不会自动结束它。','Stage Relay',0x40)
                    return
                existing = ctypes.windll.user32.FindWindowW(None, "Stage Relay")
                if existing:
                    ctypes.windll.user32.ShowWindow(existing, 9)
                    ctypes.windll.user32.SetForegroundWindow(existing)
                return
        store = Store(args.data_dir / "workspace.sqlite3")
        if not store.snapshot()["projects"]:
            store.apply("demo_create", {})
        url = f"http://127.0.0.1:{args.port}"
        try:
            server = PanelServer(("127.0.0.1",args.port),store,connection_home=args.app_home)
        except OSError:
            # The port is taken. Decide what that means before saying anything: the
            # old message said "close the old version" without naming it, so a user
            # who had just installed a new build could not tell which instance held
            # the port, and the catch-all then blamed WebView2 as well.
            try:
                with urlopen(url + "/api/health",timeout=3) as response:
                    health=json.load(response)
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    f'端口 {args.port} 已被占用，但无法读取它的健康信息（{error}）。\n'
                    f'占用者不是本应用的正常实例，或正在启动中。\n\n'
                    f'本次版本：{__version__}\n本次数据目录：{args.data_dir}') from error
            decision = decide_occupied_port(health, args.port, __version__, args.data_dir)
            if decision is None:
                # Same build, same store: attach to what is already serving.
                existing = ctypes.windll.user32.FindWindowW(None, "Stage Relay")
                if existing:
                    ctypes.windll.user32.ShowWindow(existing, 9)
                    ctypes.windll.user32.SetForegroundWindow(existing)
                return
            raise RuntimeError(decision)
        else:
            threading.Thread(target=server.serve_forever,daemon=True,name="StageRelayLocal").start()
            logging.getLogger(__name__).warning("panel data dir: %s", args.data_dir)
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True
        api = DesktopAPI()
        api._window = webview.create_window("Stage Relay", url, width=1640, height=980, min_size=(1080,720), background_color="#f7f7f6", text_select=True,js_api=api)
        webview.start(gui="edgechromium",debug=False,private_mode=False,storage_path=str(args.data_dir / "webview"),icon=str(Path(__file__).resolve().parent / "web/assets/relay.ico"))
    except Exception as error:
        logging.exception("Desktop startup failed")
        if os.name == "nt":
            # A failure importing the `webview` module does not prove the Windows
            # WebView2 Runtime is absent: that module is a Python package, and a
            # corrupted install, a missing dependency of it, or a packaging error
            # all raise ImportError too. Show the original error and keep the
            # runtime check separate from the diagnosis.
            if isinstance(error, RuntimeError) and str(error).startswith("端口"):
                message = str(error)
            elif isinstance(error, ImportError):
                message = (f"应用启动失败：无法导入 Python 界面模块 webview。\n\n"
                           f"原始错误：{error}\n\n"
                           f"这不一定是缺少 Windows WebView2 Runtime —— 该模块是 Python 包，"
                           f"安装损坏或打包缺文件也会给出同样的错误。\n"
                           f"如需确认运行时是否存在，请检查注册表项：\n"
                           f"  HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\"
                           f"{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}}\n\n"
                           f"本地日志：{args.data_dir / 'desktop.log'}")
            else:
                message = (f"应用启动失败：{error}\n\n"
                           f"本地日志：{args.data_dir / 'desktop.log'}")
            ctypes.windll.user32.MessageBoxW(None, message, "Stage Relay", 0x10)
        else:
            raise
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if mutex:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == "__main__":
    main()
