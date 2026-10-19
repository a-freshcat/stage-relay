"""Demo preparation must preserve the normal execution settings; no model calls."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
import demo


class DemoQualityTests(unittest.TestCase):
    def test_demo_does_not_lower_the_example_configuration_limits(self):
        original = json.loads((demo.ROOT / "config.example.json").read_text())
        with fixtures.WorkspaceTemp(prefix="demo-policy-") as temp:
            root = Path(temp)
            (root / "config.example.json").write_text(json.dumps(original))
            with patch.object(demo, "ROOT", root):
                _, _, configured, _ = demo.prepare()
            self.assertEqual(configured["planner"], original["planner"])
            self.assertEqual(configured["worker"], original["worker"])
            self.assertEqual(configured["limits"], original["limits"])
