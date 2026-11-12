"""The startup messages must name what is actually in the way.

A user who installed v0.1.5 and double-clicked it got "旧版本地服务正在运行，请先
关闭旧版再启动新版" — no version, no PID, no data directory — and then, because the
catch-all blamed WebView2, a second dialog telling them to install a runtime that
was already present. Neither message pointed at the real cause: an older panel
still holding port 4318.

These checks pin the wording and the branch that chooses it, without starting a
real window or touching a running instance.
"""

import ctypes
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def load_desktop():
    """Import desktop.py fresh, with the GUI side stubbed out."""
    import importlib
    sys.modules.pop("desktop", None)
    with patch.dict(sys.modules, {
        "webview": types.SimpleNamespace(settings={}, create_window=lambda *a, **k: None,
                                         start=lambda *a, **k: None),
    }):
        return importlib.import_module("desktop")


class PortConflictMessage(unittest.TestCase):
    def test_the_occupied_port_message_names_the_running_instance(self):
        desktop = load_desktop()
        health = {"app": "stage-relay", "version": "0.1.4", "pid": 27268,
                  "data_dir": r"C:\work\data"}

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""
        def fake_urlopen(url, timeout=3):
            return FakeResponse()

        with patch.object(desktop, "__version__", "0.1.5"), \
             patch.object(desktop, "urlopen", side_effect=fake_urlopen), \
             patch.object(desktop.json, "load", return_value=health), \
             patch.object(desktop, "PanelServer", side_effect=OSError(10048, "in use")), \
             patch.object(desktop, "Store"), \
             patch.object(desktop, "PanelServer", create=True):
            server_stub = MagicMock(side_effect=OSError(10048, "in use"))
            with patch.object(desktop, "PanelServer", server_stub), \
                 patch.object(desktop, "default_data_dir", return_value=Path(r"C:\work\data")), \
                 patch.object(desktop, "DesktopAPI", create=True), \
                 patch.object(sys, "argv", ["desktop.py"]), \
                 patch.object(desktop.ctypes.windll, "user32", create=True) as user32:
                try:
                    desktop.main()
                except SystemExit:
                    pass
                except Exception:
                    pass
                shown = [c.args[1] for c in user32.MessageBoxW.call_args_list if len(c.args) > 1]
        text = "\n".join(str(m) for m in shown)
        self.assertIn("占用", text, "必须说明端口被占用")
        self.assertIn("0.1.4", text, "必须写出正在运行的版本")
        self.assertIn("27268", text, "必须写出 PID")
        self.assertIn(r"C:\work\data", text, "必须写出数据目录")
        self.assertIn("不会自动结束", text, "必须说明不会自动关掉旧进程")


class FailureBlame(unittest.TestCase):
    """The catch-all must stop telling every failure to install WebView2."""

    def _message_for(self, exc):
        desktop = load_desktop()
        with patch.object(desktop, "Store"), \
             patch.object(desktop, "PanelServer", MagicMock()), \
             patch.object(desktop, "default_data_dir", return_value=Path(r"C:\work\data")), \
             patch.object(desktop, "DesktopAPI", create=True), \
             patch.object(sys, "argv", ["desktop.py"]), \
             patch.object(desktop.ctypes.windll, "user32", create=True) as user32:
            import builtins
            real_import = builtins.__import__
            def fake_import(name, *a, **k):
                if name == "webview":
                    raise exc
                return real_import(name, *a, **k)
            with patch.object(builtins, "__import__", side_effect=fake_import):
                try:
                    desktop.main()
                except Exception:
                    pass
            shown = [c.args[1] for c in user32.MessageBoxW.call_args_list if len(c.args) > 1]
        return "\n".join(str(m) for m in shown)

    def test_a_missing_webview_is_named(self):
        text = self._message_for(ImportError("No module named 'webview'"))
        self.assertIn("WebView2", text)

    def test_an_unrelated_failure_does_not_blame_webview(self):
        text = self._message_for(RuntimeError("something else broke"))
        self.assertNotIn("WebView2", text, "无关故障不能提示装 WebView2")
        self.assertIn("something else broke", text)


if __name__ == "__main__":
    unittest.main()