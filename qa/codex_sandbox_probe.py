"""Fast pass/fail repro: the real native Codex sandbox must execute a harmless echo."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from relay.codex import NativeRPC

root=Path(__file__).resolve().parent/'native-sandbox'
root.mkdir(exist_ok=True)
rpc=NativeRPC(packaged=False if '--unpackaged' in sys.argv else None,data_dir=root)
try:
    command=['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe','-NoProfile','-NonInteractive','-Command',"Write-Output 'STAGE_RELAY_SANDBOX_OK'"]
    result=rpc.request('command/exec',{'command':command,'cwd':str(root),'sandboxPolicy':{'type':'workspaceWrite','writableRoots':[str(root)],'networkAccess':False,'excludeTmpdirEnvVar':True,'excludeSlashTmp':True},'timeoutMs':5000},timeout=12)
    success=result['exitCode']==0 and 'STAGE_RELAY_SANDBOX_OK' in result['stdout']
    print(json.dumps({'sandbox_ok':success,'result':result},ensure_ascii=False))
    if not success:sys.exit(1)
except Exception as error:
    print(json.dumps({'sandbox_ok':False,'error':str(error)},ensure_ascii=False));sys.exit(1)
finally:
    rpc.close()
