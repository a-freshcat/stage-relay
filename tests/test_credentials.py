"""Disposable fixture keys only: native Windows vault and local generation mocks."""
import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_relay as fixtures
import test_settings as settings_fixtures
from relaylib.credentials import WindowsVault, CredentialError, new_reference
from relaylib.catalog import Catalog
from relaylib.gateway import resolve_endpoint
from relaylib.settings import SettingsError
from relaylib.runner import Runner,read_config
from relaylib.runtime import atomic_json


@unittest.skipUnless(os.name=="nt","Windows credential storage")
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.vault=WindowsVault();self.refs=[]
        self.addCleanup(self.cleanup_keys)

    def cleanup_keys(self):
        for reference in self.refs:self.vault.delete(reference)

    def reference(self):
        ref=new_reference();self.refs.append(ref);return ref

    def test_native_roundtrip_bound_to_endpoint_and_delete(self):
        reference=self.reference()
        self.vault.put(reference,"https://fixture.invalid/v1","fixture-key-only")
        self.assertEqual(self.vault.get(reference,"https://fixture.invalid/v1/"),"fixture-key-only")
        with self.assertRaises(CredentialError):self.vault.get(reference,"https://other.invalid/v1")
        self.vault.delete(reference)
        with self.assertRaises(CredentialError):self.vault.get(reference,"https://fixture.invalid/v1")

    def test_save_reopen_replace_and_clear_without_plaintext_files(self):
        fixture=settings_fixtures.SettingsTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        settings=fixture.settings
        payload=fixture.custom();payload["profiles"][-1]["endpoint"].pop("api_key_env")
        payload["api_keys"]={"other":"fixture-key-first"}
        view=settings.save(payload)
        ep=next(p for p in view["profiles"] if p["id"]=="other")["endpoint"]
        first=ep["api_key_ref"];self.refs.append(first)
        self.assertEqual(resolve_endpoint(ep)["headers"]["Authorization"],"Bearer fixture-key-first")
        payload={key:view[key] for key in ("revision","profiles","roles")}
        payload["api_keys"]={"other":"fixture-key-second"}
        view=settings.save(payload);ep=next(p for p in view["profiles"] if p["id"]=="other")["endpoint"]
        second=ep["api_key_ref"];self.refs.append(second)
        self.assertEqual(resolve_endpoint(ep)["headers"]["Authorization"],"Bearer fixture-key-second")
        contents=fixture.config.read_text()+settings.profiles.read_text()+json.dumps(view)
        self.assertNotIn("fixture-key-first",contents);self.assertNotIn("fixture-key-second",contents)
        payload={key:view[key] for key in ("revision","profiles","roles")}
        next(p for p in payload["profiles"] if p["id"]=="other")["endpoint"].pop("api_key_ref")
        settings.save(payload)
        with self.assertRaises(CredentialError):self.vault.get(second,"https://offline.example/v1")

    def test_stale_or_invalid_settings_never_store_keys(self):
        fixture=settings_fixtures.SettingsTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        payload=fixture.custom();payload["profiles"][-1]["endpoint"].pop("api_key_env")
        payload["api_keys"]={"other":"fixture-key-only"};payload["revision"]="stale"
        with patch("relaylib.settings.WindowsVault") as vault:
            with self.assertRaises(SettingsError):fixture.settings.save(payload)
            vault.assert_not_called()

    def test_existing_vault_key_is_available_to_worker_without_environment_variable(self):
        fixture=fixtures.RelayIntegrationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        reference=self.reference();self.vault.put(reference,fixture.upstream.url,"fixture-runtime-key")
        config=copy.deepcopy(fixture.config);config["worker"]["endpoint"]={"base_url":fixture.upstream.url,"api_key_ref":reference}
        atomic_json(fixture.sequence,{"cursor":0,"entries":[{"response":fixtures.result(),"write":{"value.txt":"fixed"}}]})
        from relaylib.plugin import expand_plan
        from test_plugin import handoff
        runner=Runner(config,fixture.project,fixture.base/"vault-run")
        state=runner.start_with_plan(expand_plan(handoff(),config["checks"]))
        self.assertEqual(state["status"],"completed")
        self.assertEqual(fixture.upstream.requests[0]["auth"],"Bearer fixture-runtime-key")
        self.assertNotIn("fixture-runtime-key",(runner.run_dir/"config.json").read_text())


class InlineDiscoveryTests(unittest.TestCase):
    def test_typed_key_discovers_before_save_and_is_not_in_response_or_cache(self):
        from test_catalog import CatalogTests
        fixture=CatalogTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        catalog=Catalog()
        with patch("relaylib.gateway.WindowsVault") as vault:
            result=catalog.discover({"endpoint":{"base_url":fixture.spec["base_url"]},"api_key":"fixture-inline-key"})
            vault.assert_not_called()
        self.assertEqual(fixture.seen[0][2],"Bearer fixture-inline-key")
        self.assertNotIn("fixture-inline-key",json.dumps(result))
        self.assertNotIn("fixture-inline-key",str(catalog.cache))


if __name__=="__main__":unittest.main()
