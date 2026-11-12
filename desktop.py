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
            # The port is taken. Find out by what before saying anything: the old
            # message said "close the old version" without naming it, so a user who
            # had just installed a new build had no way to tell which window, process
            # or data directory was in the way.
            try:
                with urlopen(url + "/api/health",timeout=3) as response:
                    health=json.load(response)
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    f'端口 {args.port} 已被占用，但无法读取它的健康信息（{error}）。\n'
                    f'占用者不是本应用的正常实例，或正在启动中。\n\n'
                    f'本次版本：{__version__}\n本次数据目录：{args.data_dir}') from error
            if health.get("app") != "stage-relay":
                raise RuntimeError(
                    f'端口 {args.port} 被其他应用占用（app={health.get("app")!r}）。\n\n'
                    f'本次版本：{__version__}')
            raise RuntimeError(
                f'端口 {args.port} 已被另一个 Stage Relay 实例占用。\n\n'
                f'正在运行：\n'
                f'  版本     {health.get("version")}\n'
                f'  PID      {health.get("pid")}\n'
                f'  端口     {args.port}\n'
                f'  数据目录 {health.get("data_dir")}\n\n'
                f'本次启动：\n'
                f'  版本     {__version__}\n'
                f'  端口     {args.port}\n'
                f'  数据目录 {args.data_dir}\n\n'
                f'请先关闭上面列出的那个实例再启动本次版本；任务和文件都会保留。\n'
                f'本应用不会自动结束它。')
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
            # Only blame WebView2 when WebView2 is actually the problem. Saying it
            # for every failure sent the user looking for a runtime that was already
            # installed, while the real cause was a port conflict.
            webview_missing = isinstance(error, ImportError) and "webview" in str(error).lower()
            if webview_missing:
                message = ("应用启动失败：缺少 Windows WebView2 Runtime。\n\n"
                           f"本地日志：{args.data_dir / 'desktop.log'}")
            elif isinstance(error, RuntimeError) and str(error).startswith("端口"):
                message = str(error)
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
