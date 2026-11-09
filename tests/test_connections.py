import json
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from pathlib import Path

from relay.connections import Connections
from relay.domain import Invalid
from relay.native_apps import dsh_runtime


ROOT = Path(__file__).resolve().parent.parent


class ConnectionSetup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-state-", dir=ROOT / "qa")
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to((ROOT / "qa").resolve())
        self.addCleanup(self.temp.cleanup)
        self.home = self.root / "fake-home"
        self.home.mkdir()
        self.connections = Connections(self.root / "data",4318,home=self.home)
        self.codex = {"id":"codex","name":"Codex","app":"codex"}
        self.anti = {"id":"antigravity","name":"Antigravity","app":"antigravity"}

    def write(self, path, source):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(source,encoding="utf-8")

    def test_an_upgrade_keeps_the_authorization_for_the_same_contract(self):
        """A new package path must not look like a revoked authorization."""
        stale = self.root / "old-package" / "StageRelayBridge.exe"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("", encoding="utf-8")
        same_args = ["--agent", "codex", "--port", "4318", "--data-dir", str(self.root / "data")]
        self.connections.owned = {"codex": {"entry": {"command": str(stale), "args": same_args}}}
        # The old bridge still exists: keep the entry the user already approved.
        self.assertEqual(self.connections.entry(self.codex)["command"], str(stale))
        # The old package was deleted by an upgrade: the same contract still holds,
        # so the current command is handed back instead of demanding re-setup.
        stale.unlink()
        entry = self.connections.entry(self.codex)
        self.assertIn(str(self.root / "data"), " ".join(entry["args"]))
        self.assertNotEqual(entry["command"], str(stale))
        # A different data store is a different contract: the stale entry must not
        # be reused just because it mentions the same agent and port.
        other = str(self.root / "other-data")
        self.connections.owned = {"codex": {"entry": {
            "command": "x", "args": ["--agent", "codex", "--port", "4318",
                                     "--data-dir", other]}}}
        entry = self.connections.entry(self.codex)
        self.assertNotEqual(entry["command"], "x")
        self.assertNotIn(other, " ".join(entry["args"]))

    def test_prepare_does_not_write_or_require_manual_config(self):
        plan = self.connections.prepare(self.codex)
        self.assertFalse((self.home / ".codex").exists())
        self.assertNotIn("command",plan)
        self.assertTrue(plan["permissions"])

    def test_explicit_consent_is_required(self):
        plan=self.connections.prepare(self.codex)
        with self.assertRaises(Invalid):
            self.connections.commit(plan["plan_id"],False)
        self.assertFalse((self.home / ".codex/config.toml").exists())

    def test_codex_backup_preserves_existing_settings(self):
        target=self.home / ".codex/config.toml"
        original='# user settings\nmodel = "keep-model"\n[mcp_servers.other]\ncommand = "keep-command"\n'
        self.write(target,original)
        plan=self.connections.prepare(self.codex)
        result=self.connections.commit(plan["plan_id"],True)
        config=tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(config["model"],"keep-model")
        self.assertEqual(config["mcp_servers"]["other"]["command"],"keep-command")
        self.assertIn("stage-relay-codex",config["mcp_servers"])
        self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"),original)
        self.assertFalse(result["verified"])

    def test_stale_plan_cannot_overwrite_concurrent_edits(self):
        target=self.home / ".codex/config.toml"
        self.write(target,'model = "one"\n')
        plan=self.connections.prepare(self.codex)
        self.write(target,'model = "two"\n')
        with self.assertRaises(Invalid):
            self.connections.commit(plan["plan_id"],True)
        self.assertEqual(tomllib.loads(target.read_text())["model"],"two")

    def test_json_merge_and_revoke_preserve_other_servers(self):
        target=self.home / ".gemini/config/mcp_config.json"
        self.write(target,json.dumps({"otherSetting":"KEEP","mcpServers":{"other":{"command":"node","env":{"TOKEN":"KEEP"}}}}))
        plan=self.connections.prepare(self.anti)
        self.connections.commit(plan["plan_id"],True)
        config=json.loads(target.read_text(encoding="utf-8"))
        config["mcpServers"]["added-later"]={"command":"keep-this-too"}
        target.write_text(json.dumps(config),encoding="utf-8")
        plan=self.connections.prepare(self.anti,remove=True)
        self.connections.commit(plan["plan_id"],True)
        result=json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(result["otherSetting"],"KEEP")
        self.assertEqual(set(result["mcpServers"]),{"other","added-later"})
        self.assertEqual(result["mcpServers"]["other"]["env"]["TOKEN"],"KEEP")

    def test_unowned_or_modified_entries_are_never_replaced(self):
        target=self.home / ".gemini/config/mcp_config.json"
        self.write(target,json.dumps({"mcpServers":{"stage-relay-antigravity":{"command":"user-command"}}}))
        with self.assertRaises(Invalid):
            self.connections.prepare(self.anti)

    def test_malformed_config_is_not_changed(self):
        target=self.home / ".gemini/config/mcp_config.json"
        self.write(target,'{ invalid json')
        with self.assertRaises(Invalid):
            self.connections.prepare(self.anti)
        self.assertEqual(target.read_text(),'{ invalid json')

    def test_reconfigure_is_idempotent_and_survives_manager_restart(self):
        plan=self.connections.prepare(self.codex)
        self.connections.commit(plan["plan_id"],True)
        manager=Connections(self.root / "data",4318,home=self.home)
        plan=manager.prepare(self.codex)
        manager.commit(plan["plan_id"],True)
        content=(self.home / ".codex/config.toml").read_text(encoding="utf-8")
        self.assertEqual(content.count('[mcp_servers.stage-relay-codex]'),1)
        plan=manager.prepare(self.codex,remove=True)
        manager.commit(plan["plan_id"],True)
        self.assertNotIn('stage-relay-codex',(self.home / ".codex/config.toml").read_text())

    def test_dsh_keeps_original_yaml_and_uses_official_client(self):
        yaml_module=Path.home() / ".dsh/profiles/desktop/node_modules/yaml"
        if not yaml_module.exists():
            self.skipTest("Installed DSH YAML parser not available")
        self.connections.yaml_module=yaml_module
        target=self.home / ".dsh/profiles/desktop/cordis.patch.yml"
        original='# existing settings\n- id: keep-plugin\n  name: keep-plugin\n  config:\n    enabled: true\n'
        self.write(target,original)
        agent={"id":"deepseek","name":"DeepSeek · DSH","app":"dsh"}
        plan=self.connections.prepare(agent)
        self.connections.commit(plan["plan_id"],True)
        source=target.read_text(encoding="utf-8")
        self.assertTrue(source.startswith(original))
        self.assertIn("@deepseek-ai/dsh-mcp-client",source)
        self.assertIn('- insert:\n  - id: stage-relay-deepseek',source)
        self.connections.validate_yaml(source)
        # Check actual DSH patch composition, not merely YAML syntax: otherwise
        # unknown plain-id rows parse successfully but never mount a plugin.
        boot=(dsh_runtime()/'node_modules/@deepseek-ai/dsh-app-boot/lib/index.js').as_uri()
        script="import {createRequire} from 'node:module';const require=createRequire(import.meta.url);const Y=require(process.argv[1]);const {composeEntries}=await import(process.argv[2]);let source='';for await(const chunk of process.stdin)source+=chunk;const rows=composeEntries([[{insert:[{id:'keep-plugin',name:'keep-plugin',config:{enabled:false}}]}],Y.parse(source)]);process.stdout.write(JSON.stringify(rows.map(r=>({id:r.id,enabled:r.config.enabled}))));"
        composition=subprocess.run(['node','--input-type=module','-e',script,str(yaml_module),boot],input=source,encoding='utf8',capture_output=True,timeout=15)
        self.assertEqual(composition.returncode,0,composition.stderr)
        rows=json.loads(composition.stdout)
        self.assertEqual([r['id'] for r in rows],['keep-plugin','stage-relay-deepseek','stage-relay-deepseek-native'])
        self.assertTrue(rows[0]['enabled'])
        plan=self.connections.prepare(agent,remove=True)
        self.connections.commit(plan["plan_id"],True)
        self.assertEqual(target.read_text(encoding="utf-8"),original+'\n')

    def test_frozen_application_config_uses_packaged_bridge(self):
        folder=self.root / "portable-app"
        folder.mkdir()
        bridge=folder / "StageRelayBridge.exe"
        bridge.touch()
        with patch("sys.frozen",True,create=True), patch("sys.executable",str(folder / "Stage Relay.exe")):
            entry=self.connections.entry(self.codex)
        self.assertEqual(entry["command"],str(bridge))
        self.assertEqual(entry["args"][:2],["--agent","codex"])
        self.assertFalse(any("bridge.py" in arg for arg in entry["args"]))


if __name__ == "__main__":
    unittest.main()
