import json
import os
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from relay.codex import CodexHost, NativeRPC
from relay.domain import Invalid, Store
from relay.runtime import Runtime
from relay.native_apps import codex_executable


ROOT=Path(__file__).resolve().parent.parent


class FakeRPC:
    def __init__(self):self.sent=[]
    def send(self,value):self.sent.append(value)


class NativeDiscovery(unittest.TestCase):
    def test_chatgpt_package_is_found_without_cli_path(self):
        with tempfile.TemporaryDirectory(prefix='test-state-',dir=ROOT/'qa') as directory:
            package=Path(directory)/'OpenAI.Codex_1_x64__publisher'
            executable=package/'app/resources/codex.exe';executable.parent.mkdir(parents=True)
            executable.touch();(package/'AppxManifest.xml').touch()
            with patch('relay.native_apps.registered_openai_packages',return_value=[package]),patch('relay.native_apps.shutil.which',return_value=None):
                self.assertEqual(codex_executable(),str(executable))

    def test_command_script_does_not_hide_installed_desktop_harness(self):
        with tempfile.TemporaryDirectory(prefix='test-state-',dir=ROOT/'qa') as directory:
            package=Path(directory)/'package';executable=package/'app/resources/codex.exe'
            executable.parent.mkdir(parents=True);executable.touch();(package/'AppxManifest.xml').touch()
            with patch('relay.native_apps.registered_openai_packages',return_value=[package]),patch('relay.native_apps.shutil.which',return_value='C:/npm/codex.cmd'):
                self.assertEqual(codex_executable(),str(executable))


class CodexNativeEvents(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='test-state-',dir=ROOT/'qa');self.addCleanup(self.temp.cleanup)
        store=Store(Path(self.temp.name)/'state.sqlite3');runtime=Runtime(store)
        p=store.apply('project_create',{'title':'原生事件验证','workspace':self.temp.name})
        runtime.poll('codex',{'sessions':[{'id':'native-codex','workspace':self.temp.name}]})
        runtime.bind(p['id'],'codex','native-codex')
        host=CodexHost.__new__(CodexHost)
        host.agent='codex';host.server=type('Server',(),{'runtime':runtime})()
        host.rpc=FakeRPC();host.sessions={'native-codex':{'status':'running'}};host.active={'native-codex':'turn-1'}
        host.approvals={};host.seq={};host.lock=threading.RLock();host.error=''
        self.host=host;self.runtime=runtime

    def test_native_permission_is_pending_until_user_decides(self):
        message={'id':9,'method':'item/commandExecution/requestApproval','params':{'threadId':'native-codex','command':'read fixture','reason':'needs permission'}}
        self.host._notification(message)
        self.assertEqual(self.host.rpc.sent,[])
        key=next(iter(self.host.approvals))
        self.host.respond(key,{'decision':'decline'})
        self.assertEqual(self.host.rpc.sent,[{'id':9,'result':{'decision':'decline'}}])
        with self.assertRaises(Invalid):self.host.respond(key,{'decision':'accept'})

    def test_unknown_thread_cannot_be_mistaken_for_our_member(self):
        self.host._notification({'id':3,'method':'item/fileChange/requestApproval','params':{'threadId':'someone-elses-thread'}})
        self.assertEqual(self.host.approvals,{})
        self.assertIn('error',self.host.rpc.sent[0])

    def test_completed_native_command_preserves_actual_result(self):
        self.host._notification({'method':'item/completed','params':{'threadId':'native-codex','item':{'id':'command-1','type':'commandExecution','command':'read fixture','status':'completed','aggregatedOutput':'verified-native-coordination'}}})
        entry=self.runtime.snapshot()['activity'][0]
        self.assertEqual(entry['type'],'tool/result');self.assertFalse(entry['data']['isError'])
        self.assertEqual(entry['data']['output'],'verified-native-coordination')

    def test_native_failure_stops_running_status_and_clears_prompts(self):
        self.host.approvals={'a':{'session':'native-codex'}}
        self.host._notification({'method':'turn/completed','params':{'threadId':'native-codex','turn':{'id':'turn-1','status':'failed','error':{'message':'native error'}}}})
        self.assertEqual(self.host.sessions['native-codex']['status'],'idle')
        self.assertEqual(self.host.approvals,{})
        self.assertEqual(self.runtime.snapshot()['activity'][0]['data']['reason']['kind'],'error')

    def test_device_verification_is_never_fabricated(self):
        self.host._notification({'id':7,'method':'mcpServer/elicitation/request','params':{'threadId':'native-codex','mode':'openai/userVerification','challenge':'fixture'}})
        key=next(iter(self.host.approvals))
        with self.assertRaises(Invalid):self.host.respond(key,{'decision':'accept'})
        self.assertEqual(self.host.rpc.sent,[])
        self.host.respond(key,{'decision':'decline'})
        self.assertEqual(self.host.rpc.sent,[{'id':7,'result':{'action':'decline'}}])

    def test_native_clock_uses_protocol_unix_seconds(self):
        self.host._notification({'id':11,'method':'currentTime/read','params':{}})
        value=self.host.rpc.sent[0]['result']['currentTimeAt']
        self.assertIsInstance(value,int)
        self.assertGreater(value,1700000000)


@unittest.skipUnless(os.environ.get('RELAY_TEST_NATIVE_CODEX')=='1','Requires explicitly enabled installed native Codex integration')
class RealCodexSandbox(unittest.TestCase):
    def test_installed_harness_runs_inside_its_native_sandbox(self):
        with tempfile.TemporaryDirectory(prefix='test-state-',dir=ROOT/'qa') as directory:
            # Match an Explorer-launched exe, not the assistant's injected PATH.
            with patch.dict(os.environ,{'PATH':'C:/Windows/System32;C:/Windows'}):
                rpc=NativeRPC(data_dir=directory)
            try:
                result=rpc.request('command/exec',{'command':['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe','-NoProfile','-NonInteractive','-Command',"Write-Output 'STAGE_RELAY_SANDBOX_OK'"],'cwd':directory,'sandboxPolicy':{'type':'workspaceWrite','writableRoots':[directory],'networkAccess':False,'excludeTmpdirEnvVar':True,'excludeSlashTmp':True},'timeoutMs':5000},timeout=12)
                self.assertEqual(result['exitCode'],0,result['stderr'])
                self.assertIn('STAGE_RELAY_SANDBOX_OK',result['stdout'])
            finally:rpc.close()


if __name__=='__main__':unittest.main()
