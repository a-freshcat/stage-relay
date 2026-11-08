"""Exercise the actual GUI release's HTTP rejection path using isolated data."""
import http.client
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

root=Path(__file__).resolve().parent.parent
exe=Path(sys.argv[1]).resolve()
assert exe.is_relative_to((root/'release').resolve()) and exe.is_file()
directory=root/'qa/desktop-v31-data';directory.mkdir(exist_ok=True)
source=sqlite3.connect(exe.parent/'data/workspace.sqlite3');target=sqlite3.connect(directory/'workspace.sqlite3');source.backup(target);target.close();source.close()
startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
process=subprocess.Popen([str(exe),'--port','4344','--data-dir',str(directory),'--app-home',str(directory/'fake-home')],startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW)
try:
    until=time.monotonic()+20
    while True:
        try:
            c=http.client.HTTPConnection('127.0.0.1',4344,timeout=2);c.request('GET','/api/health');r=c.getresponse();health=json.loads(r.read());c.close();break
        except OSError:
            if time.monotonic()>until:raise
            time.sleep(.2)
    assert health['version']=='0.3.1-codex-dsh',health
    c=http.client.HTTPConnection('127.0.0.1',4344,timeout=3);c.request('GET','/');r=c.getresponse();r.read();cookie=r.getheader('Set-Cookie').split(';')[0];c.close()
    for _ in range(12):
        c=http.client.HTTPConnection('127.0.0.1',4344,timeout=3)
        c.request('POST','/api/action',json.dumps({'action':'pause','args':{},'padding':'x'*131072}),{'Cookie':cookie,'Content-Type':'application/json'})
        r=c.getresponse();assert r.status==403;assert 'error' in json.loads(r.read());c.close()
    print(json.dumps({'release_version':health['version'],'complete_forbidden_responses':12,'actual_exe_checked':True}))
finally:
    process.terminate()
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
