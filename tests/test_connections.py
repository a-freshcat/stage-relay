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
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to((ROOT / "qa").resolve())
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
        # Same contract, but the entry must follow the running package: the bridge
        # binary carries the tool catalogue, so a pinned old path would keep
        # advertising old tools no matter how often the panel is reloaded.
        self.assertEqual(self.connections.entry(self.codex), self.connections.current_entry(self.codex))
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

    def test_a_stale_bridge_path_is_repointed_without_reauthorizing(self):
        """Upgrading must actually fix the file, not only the value we report."""
        target = self.home / ".codex/config.toml"
        self.write(target, "model = \"keep\"\n")
        stale = self.root / "old-package" / "StageRelayBridge.exe"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("", encoding="utf-8")
        self.connections.owned = {"codex": {"entry": {
            "command": str(stale),
            "args": ["--agent", "codex", "--port", "4318", "--data-dir", str(self.root / "data")]}}}
        result = self.connections.refresh(self.codex)
        self.assertTrue(result["configured"])
        written = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(written["model"], "keep")
        command = written["mcp_servers"]["stage-relay-codex"]["command"]
        self.assertNotEqual(command, str(stale))
        self.assertEqual(command, self.connections.current_entry(self.codex)["command"])
        # Already current: refreshing again writes nothing and touches no file.
        self.assertIsNone(self.connections.refresh(self.codex))
        # No recorded authorization at all: nothing to repoint.
        self.connections.owned = {}
        self.assertIsNone(self.connections.refresh(self.codex))

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
        # The commit now reads the file back; `verified` means the read-back
        # check passed, not that the app has picked the bridge up yet.
        self.assertTrue(result["verified"])

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

    def test_an_unowned_entry_is_only_replaced_with_an_exact_adoption(self):
        """Adopting is allowed, but only for the exact entry that was inspected.

        A stale bridge left behind by an older build is not something the panel
        may overwrite on its own, and a blanket approval would be
        indistinguishable from silently replacing whatever happens to be there.
        """
        import hashlib, json
        target=self.home / ".gemini/config/mcp_config.json"
        stale={"command":"C:/old/StageRelayBridge.exe","args":["--agent","antigravity"]}
        self.write(target,json.dumps({"mcpServers":{"stage-relay-antigravity":stale,"other":{"command":"x"}}}))
        digest=hashlib.sha256(json.dumps(stale,sort_keys=True,ensure_ascii=False).encode("utf-8")).hexdigest()
        # A wrong server name is refused.
        with self.assertRaises(Invalid):
            self.connections.prepare(self.anti,adopt={"server":"someone-else","digest":digest})
        # A stale digest is refused: the caller did not inspect what is on disk now.
        with self.assertRaises(Invalid):
            self.connections.prepare(self.anti,adopt={"server":"stage-relay-antigravity","digest":"0"*64})
        # The exact adoption is accepted and touches only that one entry.
        plan=self.connections.prepare(self.anti,adopt={"server":"stage-relay-antigravity","digest":digest})
        self.connections.commit(plan["plan_id"],True)
        after=json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(after["other"],{"command":"x"})
        self.assertNotEqual(after["stage-relay-antigravity"],stale)

    def test_a_write_that_fails_read_back_is_rolled_back_and_not_recorded(self):
        """Ownership is only recorded after the file proves what was written.

        If the read-back disagrees, the journal must stay untouched: claiming a
        successful adoption we cannot demonstrate is exactly the failure mode
        that would leave an unusable bridge behind.
        """
        target=self.home / ".gemini/config/mcp_config.json"
        original=json.dumps({"mcpServers":{"other":{"command":"keep"}}})
        self.write(target,original)
        plan=self.connections.prepare(self.anti)
        before=json.loads(self.connections.journal.read_text(encoding="utf-8")) if self.connections.journal.exists() else {}
        self.connections.verify_written=lambda *a, **k: "注入的校验失败"
        with self.assertRaises(Invalid):
            self.connections.commit(plan["plan_id"],True)
        # Rolled back because the file was still exactly what this commit wrote.
        self.assertEqual(target.read_text(encoding="utf-8"),original)
        after=json.loads(self.connections.journal.read_text(encoding="utf-8")) if self.connections.journal.exists() else {}
        self.assertEqual(after,before)
        self.assertNotIn("antigravity",after)

    def test_a_third_party_edit_after_our_write_is_not_rolled_back(self):
        """Rollback must never discard someone else's newer edit."""
        target=self.home / ".gemini/config/mcp_config.json"
        self.write(target,json.dumps({"mcpServers":{}}))
        plan=self.connections.prepare(self.anti)
        self.connections.verify_written=lambda *a, **k: "注入的校验失败"
        # Simulate a third party rewriting the file after our write.
        def fake_replace(temp, dest):
            dest.write_text(json.dumps({"mcpServers":{"someone":"else"}}),encoding="utf-8")
        import relay.connections as mod
        original_replace=mod.atomic_replace
        mod.atomic_replace=fake_replace
        try:
            with self.assertRaises(Invalid):
                self.connections.commit(plan["plan_id"],True)
        finally:
            mod.atomic_replace=original_replace
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["mcpServers"],{"someone":"else"})

    def test_ownership_written_by_another_process_is_seen_without_restart(self):
        """A running panel must not treat a legitimate adoption as a stranger's.

        The journal is the shared record. Holding the first reading in memory made
        a one-shot adoption look like a conflicting third-party edit, and the user
        saw a conflict prompt that no retry could clear.
        """
        import json
        target = self.home / ".gemini/config/mcp_config.json"
        self.write(target, json.dumps({"mcpServers": {}}))
        # Another process adopts the entry through the same manager code.
        other = Connections(self.root / "data", 4318, home=self.home)
        plan = other.prepare(self.anti)
        other.commit(plan["plan_id"], True)
        # This manager was created before that adoption and has its old view.
        self.connections.owned = {}
        plan = self.connections.prepare(self.anti)
        self.connections.commit(plan["plan_id"], True)
        after = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(after["stage-relay-antigravity"], self.connections.entry(self.anti))

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
