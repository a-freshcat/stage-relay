"""Read-only capability discovery on the actual installed native Codex harness."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from relay.codex import NativeRPC

rpc=NativeRPC()
try:
    account=rpc.request('account/read',{})
    models=rpc.request('model/list',{'limit':50})
    print(json.dumps({'native':True,'account_present':bool(account.get('account')),'model_count':len(models.get('data',[])),'models':[{'id':m.get('id'),'model':m.get('model'),'default':m.get('isDefault')} for m in models.get('data',[])]},ensure_ascii=False))
finally:
    rpc.close()
