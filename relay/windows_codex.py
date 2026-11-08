"""Launch the installed MSIX Codex with its real package identity intact."""
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from multiprocessing.connection import Listener


class _Input:
    def __init__(self,owner):self.owner=owner
    def write(self,text):self.owner._send({'action':'stdin','text':text})
    def flush(self):pass
    def close(self):self.owner._send({'action':'eof'})


class _Output:
    def __init__(self):self.queue=queue.Queue()
    def __iter__(self):return self
    def __next__(self):
        line=self.queue.get()
        if line is None:raise StopIteration
        return line


class PackagedCodexProcess:
    def __init__(self,executable,data_dir):
        package=Path(executable).parents[2]
        manifest=ET.parse(package/'AppxManifest.xml').getroot()
        identity=next(e for e in manifest if e.tag.endswith('Identity'))
        family=identity.attrib['Name']+'_'+package.name.rsplit('_',1)[1]
        app=next(e for e in manifest.iter() if e.tag.endswith('Application') and e.attrib.get('Id')=='App')
        name=r'\\.\pipe\stage-relay-codex-'+secrets.token_hex(16)
        folder=Path(data_dir)/'native-ipc';folder.mkdir(parents=True,exist_ok=True)
        key_file=folder/(secrets.token_hex(16)+'.key')
        key=secrets.token_bytes(32);key_file.write_bytes(key)
        self.listener=Listener(name,family='AF_PIPE',authkey=key)
        self.connection=None
        self.stdout=_Output();self.stderr=_Output();self.stdin=_Input(self)
        self.send_lock=threading.Lock();self.finished=threading.Event();self.ready=threading.Event()
        self.returncode=None;self.info={};self.pid=None
        def receive():
            try:
                self.connection=self.listener.accept()
                while True:
                    value=json.loads(self.connection.recv_bytes(32*1024*1024))
                    stream=value.get('stream')
                    if stream=='ready':self.info=value;self.pid=value['pid'];self.ready.set()
                    elif stream in ('stdout','stderr'):getattr(self,stream).queue.put(value['line'])
                    elif stream=='exit':self.returncode=value['code'];break
            except (EOFError,OSError):
                if self.returncode is None:self.returncode=-1
            finally:
                if self.connection:self.connection.close()
                self.listener.close()
                self.stdout.queue.put(None);self.stderr.queue.put(None);self.finished.set();self.ready.set()
        threading.Thread(target=receive,daemon=True).start()
        if getattr(sys,'frozen',False):
            command=str(Path(sys.executable).resolve())
            worker_args=['--codex-native-worker']
        else:
            command=str(Path(sys.executable).with_name('pythonw.exe'))
            worker_args=[str(Path(__file__).with_name('codex_worker.py'))]
        worker_args+=['--pipe',name,'--key-file',str(key_file),'--codex',str(executable)]
        literal=lambda text:"'"+str(text).replace("'","''")+"'"
        script='Invoke-CommandInDesktopPackage -PackageFamilyName '+literal(family)+' -AppId '+literal(app.attrib['Id'])+' -Command '+literal(command)+' -Args '+literal(subprocess.list2cmdline(worker_args))+' -PreventBreakaway'
        powershell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        try:
            result=subprocess.run([str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden','-Command',script],capture_output=True,text=True,timeout=12,creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:raise RuntimeError('Codex package-context launch failed: '+result.stderr.strip()[:1000])
            if not self.ready.wait(15) or not self.info:raise RuntimeError('Codex package-context relay did not connect')
            if not self.info.get('childPackage'):raise RuntimeError('Native Codex process still has no package identity')
        except Exception:
            self.terminate();self.listener.close();raise
        finally:
            key_file.unlink(missing_ok=True)

    def _send(self,value):
        if self.finished.is_set() or not self.connection:raise BrokenPipeError('Native relay disconnected')
        with self.send_lock:self.connection.send_bytes(json.dumps(value,ensure_ascii=False).encode('utf8'))

    def poll(self):return self.returncode if self.finished.is_set() else None
    def wait(self,timeout=None):
        if not self.finished.wait(timeout):raise subprocess.TimeoutExpired('native Codex relay',timeout)
        return self.returncode
    def terminate(self):
        try:self._send({'action':'stop'})
        except OSError:pass
    def kill(self):
        self.terminate()
        if self.connection:self.connection.close()
        self.listener.close()
