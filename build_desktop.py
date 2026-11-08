"""Build a portable Windows application in a new, never-overwritten directory."""

import shutil
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from relay import __version__

ROOT = Path(__file__).resolve().parent


def main():
    if sys.platform != "win32":
        raise SystemExit("Build this Windows executable on Windows.")
    output = ROOT / ('release/StageRelay-'+__version__)
    if output.exists():
        output = output.with_name(output.name + "-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True)
    assert output.resolve().is_relative_to((ROOT / "release").resolve())
    work = ROOT / "build" / output.name
    common = [sys.executable,"-m","PyInstaller","--noconfirm","--specpath",str(work),"--workpath",str(work / "work")]
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / "build/pyinstaller-cache"))
    subprocess.run(common + ["--onedir","--windowed","--name","Stage Relay","--distpath",str(output),"--add-data",f"{ROOT / 'web'};web","--add-data",f"{ROOT / 'integrations'};integrations","--collect-data","webview","--hidden-import","clr_loader","--exclude-module","PyQt5","--exclude-module","PyQt6","--exclude-module","PySide6","--icon",str(ROOT / "web/assets/relay.ico"),str(ROOT / "desktop.py")],check=True,cwd=ROOT,env=env)
    folder = output / "Stage Relay"
    subprocess.run(common + ["--onefile","--console","--name","StageRelayBridge","--distpath",str(folder),str(ROOT / "bridge.py")],check=True,cwd=ROOT,env=env)
    shutil.copy2(ROOT / "README.md",folder / "使用说明.md")
    print(folder / "Stage Relay.exe",flush=True)


if __name__ == "__main__":
    main()
