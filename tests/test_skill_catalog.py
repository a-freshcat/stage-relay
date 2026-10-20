"""Skill discovery, lazy routing and evidence use local files and fake models only."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
from relaylib import skill_catalog
from relaylib.runner import Runner
from relaylib.runtime import atomic_json
from panel import Store


class SkillCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = fixtures.WorkspaceTemp("skills-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.project = self.home / "project"
        self.project.mkdir()
        self.codex = self.home / "native"
        self.codex.mkdir()

    def skill(self, folder, name="svg", description="Build SVG animation", body="PRIVATE_FULL_BODY", header=""):
        path = folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n{header}---\n{body}\n", encoding="utf-8")
        return path

    def discover(self):
        return skill_catalog.discover(self.project, home=self.home, codex_home=self.codex)

    def test_gpt_never_gets_added_catalog_and_unknown_alias_is_conservative(self):
        for model in ("gpt-6.1-sol", "openai/gpt-6", "gpt6", "codex-mini", "o3", "o4-mini", "proxy/gpt-6.1-sol"):
            self.assertFalse(skill_catalog.policy({"model": model, "skill_reuse": "non_gpt"})["enabled"])
        self.assertFalse(skill_catalog.policy({"model": "gateway-alias"})["enabled"])
        self.assertFalse(skill_catalog.policy({"model": "deepseek-flash", "skill_reuse": "off"})["enabled"])
        self.assertTrue(skill_catalog.policy({"model": "deepseek-flash"})["enabled"])
        self.assertTrue(skill_catalog.policy({"model": "gateway-alias", "skill_reuse": "non_gpt"})["enabled"])

    def test_project_precedence_nested_user_skills_and_multiline_metadata(self):
        project = self.skill(self.project / ".agents/skills/svg")
        self.skill(self.home / ".agents/skills/svg", description="User duplicate")
        self.skill(self.codex / "skills/.system/helper", "helper", ">\n  Multi line\n  description")
        rows = {row["id"]: row for row in self.discover()["entries"]}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["svg"]["path"], str(project.resolve()))
        self.assertEqual(rows["helper"]["description"], "Multi line description")

    def test_disabled_skill_is_not_reexposed(self):
        path = self.skill(self.codex / "skills/disabled")
        (self.codex / "config.toml").write_text('[[skills.config]]\npath=' + json.dumps(str(path)) + '\nenabled=false\n')
        self.assertEqual(self.discover()["entries"], [])

    def test_only_enabled_plugin_latest_cache_with_valid_path(self):
        for version in ("1.9.0", "1.10.0"):
            self.skill(self.codex / f"plugins/cache/local/good/{version}/skills/svg", description=version)
        self.skill(self.codex / "plugins/cache/local/off/2.0.0/skills/off", "off")
        bad = self.codex / "plugins/cache/local/bad/1.0.0"
        (bad / ".codex-plugin").mkdir(parents=True)
        (bad / ".codex-plugin/plugin.json").write_text('{"skills":"../../../../../../"}')
        (self.codex / "config.toml").write_text('[plugins."good@local"]\nenabled=true\n[plugins."off@local"]\nenabled=false\n[plugins."bad@local"]\nenabled=true\n')
        catalog = self.discover()
        self.assertEqual(len(catalog["entries"]), 1)
        self.assertEqual(catalog["entries"][0]["id"], "good@local:svg")
        self.assertEqual(catalog["entries"][0]["description"], "1.10.0")
        self.assertTrue(catalog["warnings"])

    def test_plugin_declared_skill_directory_and_explicit_local_path(self):
        root = self.codex / "plugins/cache/local/custom/1.0.0"
        self.skill(root / "workflows/one", "one")
        (root / "plugin.json").write_text('{"skills":"./workflows"}')
        path = self.skill(self.home / "extra", "extra")
        (self.codex / "config.toml").write_text('[plugins."custom@local"]\nenabled=true\n[[skills.config]]\npath=' + json.dumps(str(path)) + '\nenabled=true\n')
        self.assertEqual({r["name"] for r in self.discover()["entries"]}, {"one", "extra"})

    def test_dependencies_are_hints_never_a_claim_of_tools_or_connectivity(self):
        self.skill(self.codex / "skills/browser", body="mcp__browser__open cua.getState() scripts/check.py",
                   header="dependencies:\n  tools:\n    - type: mcp\n      value: browser\n")
        (self.codex / "config.toml").write_text('[mcp_servers.browser]\ncommand="unused"\n')
        row = self.discover()["entries"][0]
        self.assertEqual(row["dependencies"], ["browser"])
        self.assertIn("连接未验证", " ".join(row["hints"]))
        self.assertIn("宿主工具", " ".join(row["hints"]))

    def test_bad_metadata_and_invalid_optional_config_do_not_break_discovery(self):
        self.skill(self.codex / "skills/missing", name="")
        self.skill(self.codex / "skills/good", name="good")
        (self.codex / "config.toml").write_text('skills="invalid"\nplugins=[]\n')
        catalog = self.discover()
        self.assertEqual([r["name"] for r in catalog["entries"]], ["good"])
        self.assertTrue(catalog["warnings"])

    def test_lazy_prompt_has_recommendations_and_full_index_not_skill_bodies(self):
        self.skill(self.codex / "skills/svg")
        self.skill(self.codex / "skills/python", "python", "Python tests")
        catalog = self.discover()
        with patch.object(skill_catalog, "discover", return_value=catalog) as scan:
            saved = skill_catalog.prepare(self.project, self.home, "Build SVG", {})
            again = skill_catalog.prepare(self.project, self.home, "Build SVG", {})
            self.assertEqual(scan.call_count, 1)
        self.assertEqual(saved, again)
        self.assertEqual(saved["recommended"], ["svg"])
        text = skill_catalog.prompt(saved, self.home / "worker-skills.json")
        self.assertIn("SKILL.md", text)
        self.assertIn("worker-skills.json", text)
        self.assertNotIn("PRIVATE_FULL_BODY", text)
        self.assertNotIn('"id":"python"', text)
        self.assertIn("not an allowlist", text)
        self.assertIn("approval/sandbox", text)
        self.assertEqual(len(saved["entries"]), 2)

    def test_read_commands_distinguish_failure_echo_path_suffix_and_live_tail(self):
        path = self.skill(self.codex / "skills/svg")
        catalog = self.discover()
        events = []
        for command, code in [(f'Get-Content "{path}"', 1), (f'echo "Get-Content {path}"', 0),
                              (f'Get-Content "{path}.bak"', 0), (f'pwsh -Command "Get-Content \'{path}\'"', 0)]:
            events.append({"type": "item.completed", "item": {"id": str(len(events)), "type": "command_execution",
                           "command": command, "exit_code": code, "status": "completed"}})
        log = self.home / "events.jsonl"
        log.write_text("\n".join(json.dumps(e) for e in events))
        reads = skill_catalog.read_evidence(log, catalog)
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["event_id"], "3")
        self.assertEqual(skill_catalog.read_evidence(log, catalog, recent_bytes=10), [])
        events[-1]["item"]["command"] = events[-1]["item"]["command"].replace("\\", "\\\\")
        log.write_text(json.dumps(events[-1]))
        self.assertEqual(len(skill_catalog.read_evidence(log, catalog)), 1)

    def test_generic_skill_words_and_exclusion_clause_do_not_recommend_unrelated_skills(self):
        self.skill(self.codex / "skills/imagegen", "imagegen", "Create raster photos. Do not use for SVG.")
        self.skill(self.codex / "skills/installer", "installer", "Install existing skills for users")
        self.skill(self.codex / "skills/svg", "svg", "Draw SVG")
        with patch.object(skill_catalog, "discover", return_value=self.discover()):
            catalog = skill_catalog.prepare(self.project, self.home, "Use existing skill for SVG", {"goal": "SVG"})
        self.assertEqual(catalog["recommended"], ["svg"])


class SkillRelayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RelayIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config["worker"]["model"] = "deepseek-fixture"
        self.run_dir = self.fixture.base / "skill-run"

    def test_gpt_stage_skips_discovery_and_added_prompt_entirely(self):
        self.fixture.config["worker"]["model"] = "gpt-6.1-sol"
        atomic_json(self.fixture.sequence, {"cursor": 0, "entries": [
            {"response": fixtures.result(), "write": {"value.txt": "fixed"}}]})
        runner = Runner(self.fixture.config, self.fixture.project, self.run_dir)
        with patch.object(skill_catalog, "discover", side_effect=AssertionError("GPT must not scan skills")):
            state = runner.start_with_plan(fixtures.plan(), "Fix value")
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["skill_policy"]["enabled"])
        self.assertFalse((self.run_dir / "worker-skills.json").exists())
        self.assertNotIn("REUSABLE LOCAL SKILLS", (self.run_dir / "001-worker/input.txt").read_text(encoding="utf-8"))

    def test_worker_reads_existing_skill_and_panel_shows_observed_evidence(self):
        path = self.fixture.base / "existing/SKILL.md"
        path.parent.mkdir()
        path.write_text("existing skill", encoding="utf-8")
        row = {"id": "existing", "name": "existing", "description": "Fix value", "path": str(path), "source": "user", "hints": []}
        catalog = {"version": 1, "entries": [row], "warnings": [], "note": "fixture"}
        event = {"type": "item.completed", "item": {"id": "read1", "type": "command_execution", "exit_code": 0,
                  "command": 'Get-Content "' + str(path) + '"', "status": "completed"}}
        atomic_json(self.fixture.sequence, {"cursor": 0, "entries": [{"response": fixtures.result(),
                    "events": [event], "write": {"value.txt": "fixed"}}]})
        runner = Runner(self.fixture.config, self.fixture.project, self.run_dir)
        with patch.object(skill_catalog, "discover", return_value=catalog):
            state = runner.start_with_plan(fixtures.plan(), "Fix value")
        self.assertEqual(state["status"], "completed")
        prompt = (self.run_dir / "001-worker/input.txt").read_text(encoding="utf-8")
        self.assertIn(str(path).replace("\\", "\\\\"), prompt)
        self.assertEqual(len(self.fixture.upstream.requests), 1)
        view = Store(self.fixture.base).snapshot(Store(self.fixture.base).identity(self.run_dir))
        self.assertEqual(view["skill_catalog"]["entries"][0]["id"], "existing")
        self.assertEqual(view["phases"][0]["skill_reads"][0]["id"], "existing")
        self.assertFalse((self.fixture.project / "worker-skills.json").exists())

    def test_planning_and_format_repair_do_not_receive_worker_skill_instructions(self):
        atomic_json(self.fixture.sequence, {"cursor": 0, "entries": [
            {"response": fixtures.plan()}, {"invalid_json": True, "write": {"value.txt": "fixed"}},
            {"response": fixtures.result()}]})
        runner = Runner(self.fixture.config, self.fixture.project, self.run_dir)
        with patch.object(skill_catalog, "discover", return_value={"entries": [], "warnings": []}):
            state = runner.start("Fix value")
        self.assertEqual(state["status"], "completed")
        for name in ("001-planner", "003-worker"):
            self.assertNotIn("REUSABLE LOCAL SKILLS", (self.run_dir / name / "input.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
