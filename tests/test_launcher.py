"""The daily launcher must tell "code version" from "the version actually running".

A user double-clicked the new build, got a warning about the old version, and had
no way to tell whether the panel they could open was upgraded. "The page opens"
is not the same as "the new code is running", and the launcher has to say which
one happened without asking the user to pick a folder under release/.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "启动 Stage Relay.cmd"


class Launcher(unittest.TestCase):
    def setUp(self):
        self.text = LAUNCHER.read_text(encoding="utf-8")

    def test_there_is_one_launcher_and_it_is_in_the_project_root(self):
        found = list(ROOT.glob("*.cmd"))
        self.assertIn(LAUNCHER, found, "启动器必须留在项目根目录")
        others = [p for p in found if p.name != LAUNCHER.name and "Stage Relay" in p.name]
        self.assertEqual(others, [], "不要再加第二份启动器")

    def test_it_never_sends_the_user_into_release(self):
        for needle in ("release\\", "release/", "StageRelay-0.1", "StageRelay-0.3"):
            self.assertNotIn(needle, self.text,
                             "启动器不能让用户在 release 下挑文件夹")

    def test_it_prints_the_code_version(self):
        self.assertIn("relay import __version__", self.text,
                      "必须打印当前代码的版本号")

    def test_it_runs_the_root_app_entry(self):
        self.assertIn("app.py", self.text)
        self.assertIn("--open", self.text, "已运行时应打开现有 Web")

    def test_a_nonzero_exit_is_surfaced(self):
        self.assertIn("ERRORLEVEL", self.text.upper())
        self.assertIn("pause", self.text.lower(), "失败时窗口不能一闪而过")


class RunningInstanceWording(unittest.TestCase):
    """What app.py prints when the port is already held by another version."""

    def test_the_message_separates_this_launch_from_the_running_service(self):
        source = (ROOT / "relay" / "httpd.py").read_text(encoding="utf-8")
        self.assertIn("该实例 pid", source, "必须报出在跑的那个 pid")
        self.assertIn("本次未启动新版本", source,
                      "必须明确：这次没有启动新版本，只是打开了既有服务")
        self.assertIn("如需加载更新的代码", source,
                      "必须告诉用户怎么才能真正换成新代码")

    def test_a_healthy_store_is_not_described_as_upgraded(self):
        """The launcher must not claim an upgrade just because the page opened."""
        text = LAUNCHER.read_text(encoding="utf-8")
        for claim in ("已升级", "已更新到最新", "最新版本已生效"):
            self.assertNotIn(claim, text,
                             "『Web 能打开』不等于『已升级』，不能这样写")


if __name__ == "__main__":
    unittest.main()