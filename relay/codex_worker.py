"""Private stdio relay launched in the installed Codex package context.

Only authenticated byte messages are accepted; no pickle or arbitrary shell.
"""
import argparse
import ctypes
import json
import subprocess
import threading
from pathlib import Path
from multiprocessing.connection import Client


def package_name(handle=None):
    kernel=ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype=ctypes.c_void_p
    kernel.GetPackageFullName.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32),ctypes.c_wchar_p]
    size=ctypes.c_uint32()
    handle=handle or kernel.GetCurrentProcess()
    code=kernel.GetPackageFullName(handle,ctypes.byref(size),None)
    if code!=122:return None
    value=ctypes.create_unicode_buffer(size.value)
    return value.value if kernel.GetPackageFullName(handle,ctypes.byref(size),value)==0 else None


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--pipe',required=True)
    parser.add_argument('--key-file',required=True)
    parser.add_argument('--codex',required=True)
    args=parser.parse_args(argv)
    key=Path(args.key_file).read_bytes()
    connection=Client(args.pipe,family='AF_PIPE',authkey=key)
    child=None
    lock=threading.Lock()
    def send(value):
        with lock:connection.send_bytes(json.dumps(value,ensure_ascii=False).encode('utf8'))
    try:
        child=subprocess.Popen([args.codex,'app-server','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',bufsize=1,creationflags=subprocess.CREATE_NO_WINDOW)
        send({'stream':'ready','pid':child.pid,'workerPackage':package_name(),'childPackage':package_name(int(child._handle))})
        def pump(stream):
            for line in getattr(child,stream):send({'stream':stream,'line':line})
        for stream in ('stdout','stderr'):threading.Thread(target=pump,args=(stream,),daemon=True).start()
        def completed():
            child.wait()
            try:send({'stream':'exit','code':child.returncode})
            except (EOFError,OSError):pass
        threading.Thread(target=completed,daemon=True).start()
        while True:
            message=json.loads(connection.recv_bytes(32*1024*1024))
            action=message.get('action')
            if action=='stdin':child.stdin.write(message['text']);child.stdin.flush()
            elif action=='eof':child.stdin.close()
            elif action=='stop':break
            else:raise ValueError('Unsupported native relay operation')
    except (EOFError,OSError):
        pass
    finally:
        if child and child.poll() is None:
            try:child.stdin.close();child.wait(timeout=4)
            except (OSError,ValueError,subprocess.TimeoutExpired):
                child.terminate()
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.kill()
        connection.close()


if __name__=='__main__':main()
