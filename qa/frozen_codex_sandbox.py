"""Verify the actual GUI exe's hidden native worker preserves the Codex sandbox."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from relay.codex import NativeRPC

exe=Path(sys.argv[1]).resolve()
if not exe.is_file() or not exe.is_relative_to((ROOT/'release').resolve()):raise ValueError('Expected a built release executable')
directory=ROOT/'qa/frozen-native-data';directory.mkdir(exist_ok=True)
sys.frozen=True
sys.executable=str(exe)
rpc=NativeRPC(data_dir=directory)
try:
    result=rpc.request('command/exec',{'command':['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe','-NoProfile','-NonInteractive','-Command',"Write-Output 'STAGE_RELAY_FROZEN_SANDBOX_OK'"],'cwd':str(directory),'sandboxPolicy':{'type':'workspaceWrite','writableRoots':[str(directory)],'networkAccess':False,'excludeTmpdirEnvVar':True,'excludeSlashTmp':True},'timeoutMs':5000},timeout=12)
    assert result['exitCode']==0 and 'STAGE_RELAY_FROZEN_SANDBOX_OK' in result['stdout'],result
    print(json.dumps({'frozen_native_sandbox_ok':True,'package_identity':bool(rpc.process.info.get('childPackage')),'exit_code':result['exitCode']}))
finally:rpc.close()
