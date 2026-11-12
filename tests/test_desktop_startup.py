"""Starting the app twice must open the running window, not fail.

A change here once made every launch refuse once an instance was already running:
the port-conflict path lost its "same build, same store" branch and returned an
error unconditionally. That is a regression with a bad shape - the app simply
stopped opening, and the message blamed the wrong thing.

The decision is tested as a function (desktop.decide_occupied_port) rather than by
running main(): no window, no mutex, no log handle, and no real instance involved.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desktop  # noqa: E402


class OccupiedPort(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-state-", dir=ROOT / "qa")
        self.addCleanup(self.tmp.cleanup)
        self.here = Path(self.tmp.name).resolve()
        self.elsewhere = self.here / "other-store"

    def health(self, **over):
        base = {"app": "stage-relay", "version": "0.1.5", "pid": 4242,
                "data_dir": str(self.here)}
        base.update(over)
        return base

    def test_the_same_build_on_the_same_store_attaches(self):
        """None means: open the window that is already serving. This is the case
        that was broken - it must never produce a refusal."""
        self.assertIsNone(
            desktop.decide_occupied_port(self.health(), 4318, "0.1.5", self.here))

    def test_an_older_build_is_refused_and_named(self):
        message = desktop.decide_occupied_port(
            self.health(version="0.1.4", pid=27268), 4318, "0.1.5", self.here)
        self.assertIsNotNone(message)
        self.assertIn("0.1.4", message, "必须写出正在运行的版本")
        self.assertIn("27268", message, "必须写出占用的 PID")
        self.assertIn("另一个版本", message, "要说明是版本不同")

    def test_a_different_store_is_refused_and_shown(self):
        message = desktop.decide_occupied_port(
            self.health(data_dir=str(self.elsewhere)), 4318, "0.1.5", self.here)
        self.assertIsNotNone(message)
        self.assertIn("other-store", message, "必须写出对方加载的目录")
        self.assertIn("数据目录不同", message)

    def test_a_different_application_is_named(self):
        message = desktop.decide_occupied_port(
            self.health(app="nginx"), 4318, "0.1.5", self.here)
        self.assertIn("其他应用", message)

    def test_the_message_never_claims_it_will_close_the_other_instance(self):
        for health in (self.health(version="0.1.4"), self.health(data_dir=str(self.elsewhere))):
            message = desktop.decide_occupied_port(health, 4318, "0.1.5", self.here)
            self.assertIn("不会自动结束", message)

    def test_an_unknown_running_directory_does_not_count_as_the_same_store(self):
        """Missing data_dir must not be read as "same place" and silently attach."""
        message = desktop.decide_occupied_port(
            self.health(data_dir=None), 4318, "0.1.5", self.here)
        self.assertIsNotNone(message, "无法确认目录时必须拒绝，不能连到别人的数据")


class StartupWording(unittest.TestCase):
    """The catch-all dialog must stop blaming WebView2 for everything."""

    def test_an_import_error_shows_the_original_message(self):
        source = (ROOT / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("原始错误", source, "必须显示原始导入错误")
        self.assertIn("不一定是缺少", source,
                      "ImportError 不等于缺 Windows WebView2 Runtime")

    def test_the_runtime_check_is_separate_from_the_diagnosis(self):
        source = (ROOT / "desktop.py").read_text(encoding="utf-8")
        self.assertIn("EdgeUpdate", source, "给出可核实的运行时检测方式，而不是断言")


if __name__ == "__main__":
    unittest.main()