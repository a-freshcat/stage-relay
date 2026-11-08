"""Regression tests for the store the panel actually loads and owns.

The failure these guard against: a second launch resolved a different data
directory and, because Windows lets a second listener join a port bound with
SO_REUSEADDR, both answered on one URL. Pages then showed only whichever store
answered first -- real projects looked deleted while nothing had been deleted.
"""

import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from relay.domain import Store
from relay.httpd import PanelServer
from relay.storage import default_data_dir, store_candidates, summarize


ROOT = Path(__file__).resolve().parent.parent


def make_store(directory: Path, demo_only: bool, real_title: str = ""):
    store = Store(directory / "workspace.sqlite3")
    store.apply("demo_create", {})
    if demo_only:
        return store
    store.apply("project_create", {"title": real_title, "goal": "保留真实项目", "workspace": str(directory)})
    return store


class StorageChoice(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="storage-choice-", dir=ROOT / "qa")
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to((ROOT / "qa").resolve())

    def tearDown(self):
        self.temp.cleanup()

    def test_demo_only_dir_never_outranks_a_dir_with_real_projects(self):
        # The real failure layout: `python app.py` in the repo created a stray
        # demo-only data/ while the packaged build kept the real projects under
        # release/<version>/Stage Relay/data. Newest file used to win.
        repo = self.root / "repo"
        demo = repo / "data"
        demo.mkdir(parents=True)
        real = repo / "release" / "StageRelay-0.3.1-t" / "Stage Relay" / "data"
        real.mkdir(parents=True)
        make_store(demo, demo_only=True)
        make_store(real, demo_only=False, real_title="真实任务")
        import os, time
        time.sleep(0.01)
        os.utime(demo / "workspace.sqlite3", None)
        chosen = default_data_dir(app_dir=repo)
        self.assertEqual(chosen, real)
        self.assertEqual(summarize(chosen)["real_projects"], 1)

    def test_frozen_app_does_not_prefer_its_own_stray_data_dir(self):
        # A packaged build sits in release/StageRelay-x/Stage Relay; running it
        # must keep using the store that holds the user's projects, not create a
        # fresh demo-only one beside the exe.
        app = self.root / "release" / "StageRelay-0.9.9-t" / "Stage Relay"
        app.mkdir(parents=True)
        keep = self.root / "release" / "StageRelay-0.3.1-t" / "Stage Relay" / "data"
        keep.mkdir(parents=True)
        make_store(keep, demo_only=False, real_title="真实任务")
        chosen = default_data_dir(app_dir=app)
        self.assertEqual(chosen, keep)

    def test_env_override_wins(self):
        import os
        target = self.root / "custom"
        target.mkdir()
        previous = os.environ.get("STAGE_RELAY_DATA")
        os.environ["STAGE_RELAY_DATA"] = str(target)
        try:
            self.assertEqual(default_data_dir(app_dir=self.root), target.resolve())
        finally:
            if previous is None:
                os.environ.pop("STAGE_RELAY_DATA", None)
            else:
                os.environ["STAGE_RELAY_DATA"] = previous

    def test_candidates_include_every_release_data_dir(self):
        app = self.root / "release" / "StageRelay-0.9.9-t" / "Stage Relay"
        app.mkdir(parents=True)
        (self.root / "release" / "StageRelay-0.1.0-t" / "Stage Relay" / "data").mkdir(parents=True)
        found = store_candidates(app_dir=app)
        self.assertIn(app / "data", found)
        self.assertIn(self.root / "release" / "StageRelay-0.1.0-t" / "Stage Relay" / "data", found)


class PortOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="port-own-", dir=ROOT / "qa")
        self.dir = Path(self.temp.name)
        assert self.dir.resolve().is_relative_to((ROOT / "qa").resolve())

    def tearDown(self):
        self.temp.cleanup()

    def test_second_panel_cannot_share_a_port(self):
        (self.dir / "a").mkdir()
        (self.dir / "b").mkdir()
        first = PanelServer(("127.0.0.1", 0), make_store(self.dir / "a", True))
        port = first.server_port
        try:
            with self.assertRaises(OSError):
                PanelServer(("127.0.0.1", port), make_store(self.dir / "b", True))
        finally:
            first.server_close()
        # Releasing must not leave the port unusable for the next launch.
        again = PanelServer(("127.0.0.1", port), make_store(self.dir / "b", True))
        again.server_close()

    def test_health_reports_the_store_behind_it(self):
        import http.client
        store = make_store(self.dir, demo_only=False, real_title="真实任务")
        server = PanelServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/health")
            health = json.loads(connection.getresponse().read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(health["app"], "stage-relay")
        self.assertEqual(Path(health["data_dir"]).resolve(), self.dir.resolve())
        self.assertIsInstance(health["pid"], int)

    def test_state_carries_the_data_directory_for_the_page(self):
        import http.client
        store = make_store(self.dir, demo_only=False, real_title="真实任务")
        server = PanelServer(("127.0.0.1", 0), store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/")
            cookie = dict(connection.getresponse().getheaders())["Set-Cookie"].split(";")[0]
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/state", headers={"Cookie": cookie})
            state = json.loads(connection.getresponse().read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(Path(state["storage_path"]).resolve(), self.dir.resolve())
        titles = [project["title"] for project in state["projects"]]
        self.assertIn("真实任务", titles)


if __name__ == "__main__":
    unittest.main()