"""Inline worker selection uses the complete API catalog and preserves settings."""
import copy,json,threading,unittest,urllib.request,urllib.error
from unittest.mock import patch
import test_settings as settings_fixture
import test_catalog as catalog_fixture
import test_relay as fixtures
from panel import Store,make_server
from relaylib.catalog import Catalog
from relaylib.runner import read_config
from relaylib.runtime import atomic_json
from relaylib.credentials import CredentialError,new_reference


class WorkerPickerTests(unittest.TestCase):
    def setUp(self):
        self.s=settings_fixture.SettingsTests();self.s.setUp();self.addCleanup(self.s.tearDown)
        self.c=catalog_fixture.CatalogTests();self.c.setUp();self.addCleanup(self.c.doCleanups)
        self.c.body={'data':[{'id':'model-'+str(i)} for i in range(14)]}
        payload=self.s.payload();payload['profiles'].append({'id':'fourteen','name':'14 models','endpoint':self.c.spec,'models':['model-0','model-1','model-2']})
        self.view=self.s.settings.save(payload)

    def test_full_catalog_and_inline_change_only_worker(self):
        server=make_server(Store(self.s.root/'runs'),settings=self.s.settings)
        threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def post(route,body):
            req=urllib.request.Request(server.url+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Relay-Panel':'settings','Origin':f'http://127.0.0.1:{server.server_port}'})
            with urllib.request.urlopen(req) as response:return json.load(response)
        data=post('api/worker-models',{'provider_id':'fourteen','refresh':True})
        self.assertEqual(len(data['models']),14);self.assertEqual(data['saved_count'],3)
        before=read_config(self.s.config)
        result=post('api/worker',{'revision':self.view['revision'],'provider_id':'fourteen','model':'model-12'})
        after=read_config(self.s.config)
        self.assertEqual(after['worker']['model'],'model-12')
        for key in ('planner','limits','checks','codex_command'):self.assertEqual(before[key],after[key])
        self.assertIn('model-12',next(p for p in result['profiles'] if p['id']=='fourteen')['models'])
        self.assertTrue(all(row[0]=='GET' and row[1]=='/v1/models' for row in self.c.seen))
        self.assertNotIn('PRIVATE_FIXTURE_KEY',json.dumps(result))

    def test_unlisted_model_and_stale_config_are_rejected(self):
        before=self.s.config.read_bytes()
        with self.assertRaises(ValueError):self.s.settings.select_worker({'revision':self.view['revision'],'provider_id':'fourteen','model':'nonexistent'},Catalog())
        with self.assertRaises(ValueError):self.s.settings.select_worker({'revision':'old','provider_id':'fourteen','model':'model-0'},Catalog())
        self.assertEqual(before,self.s.config.read_bytes())

    def test_credential_status_distinguishes_readable_and_missing_without_secret(self):
        profile=next(p for p in self.view['profiles'] if p['id']=='fourteen')
        profile['endpoint'].pop('api_key_env');profile['endpoint']['api_key_ref']=new_reference()
        atomic_json(self.s.settings.profiles,{'version':1,'profiles':self.view['profiles']})
        with patch('relaylib.settings.WindowsVault') as vault:
            vault.return_value.get.return_value='NEVER_SHOW_FIXTURE_KEY'
            view=self.s.settings.view()
            self.assertEqual(view['credential_states']['fourteen'],'saved')
            self.assertNotIn('NEVER_SHOW_FIXTURE_KEY',json.dumps(view))
            vault.return_value.get.side_effect=CredentialError('missing')
            self.assertEqual(self.s.settings.view()['credential_states']['fourteen'],'missing')

class CliInitFeedbackTests(unittest.TestCase):
    def test_real_initialization_signature_is_not_reported_as_model_failure(self):
        f=fixtures.RelayIntegrationTests();f.setUp();self.addCleanup(f.doCleanups)
        def stopped(argv,cwd,prompt,timeout,max_bytes,stdout,stderr,**kwargs):
            stdout.write_text('',encoding='utf-8');stderr.write_text('Error: failed to initialize in-process app-server client: Access is denied (os error 5)',encoding='utf-8')
            return {'exit_code':1,'failure':None,'elapsed_seconds':0.2}
        atomic_json(f.sequence,{'cursor':0,'entries':[]})
        config=f.base/'config.json';atomic_json(config,f.config)
        from relaylib.runner import Runner
        with patch('relaylib.runner.run_process',side_effect=stopped):
            state=Runner(f.config,f.project,f.base/'run').start('Initialization test')
        self.assertEqual(state['failure']['code'],'cli_initialization_error')
        self.assertEqual(f.upstream.requests,[])
