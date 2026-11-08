"""Boot an isolated DSH profile that points at the candidate instance.

Uses its own DSH_HOME and an `--patch` overlay, so the user's desktop profile,
the real project binding and the published package are untouched. The Stage Relay
plugin entry is inserted at the overlay level, replacing both the bridge MCP entry
and the native plugin's panelUrl/credentialsFile.
"""
import json
import os
import shutil
import subprocess
import sys
import time

ELECTRON = r"D:\ai_ds\DeepSeek Harness.exe"
DSH_BIN = r"D:\ai_ds\resources\app\dsh\node_modules\@deepseek-ai\dsh\lib\bin.js"
SOURCE_HOME = os.path.expanduser(r"~\.dsh")
ISOLATED = os.path.abspath(r"qa\context-native-a1b2c3\dsh-home")
BRIDGE = os.path.abspath(r"release\StageRelay-0.1.2\Stage Relay\StageRelayBridge.exe")
DATA = os.path.abspath(r"qa\context-native-a1b2c3\data")
PLUGIN = "file:///" + os.path.abspath(
    r"release\StageRelay-0.1.2\Stage Relay\_internal\integrations\dsh\index.mjs").replace("\\", "/")
RUNTIME_ROOT = r"D:\ai_ds\resources\app\dsh"

os.makedirs(ISOLATED, exist_ok=True)
profiles = os.path.join(ISOLATED, "profiles")
source_profiles = os.path.join(SOURCE_HOME, "profiles")
if not os.path.isdir(profiles):
    # Only the profile definition is copied; sessions, caches and credentials stay
    # in the real home so nothing here can read or change them.
    shutil.copytree(source_profiles, profiles,
                    ignore=shutil.ignore_patterns("node_modules", "*.sqlite3*", "sessions", "cache"))
# `desktop` is owned by the running Electron app, so the isolated copy gets its
# own name. node_modules is symlinked instead of copied (several hundred MB).
source_desktop = os.path.join(profiles, "desktop")
isolated_profile = os.path.join(profiles, "relay-verify")
if os.path.isdir(source_desktop) and not os.path.isdir(isolated_profile):
    os.rename(source_desktop, isolated_profile)
    link = os.path.join(isolated_profile, "node_modules")
    if not os.path.exists(link):
        try:
            os.symlink(os.path.join(source_profiles, "desktop", "node_modules"), link, target_is_directory=True)
        except OSError as exc:
            print("symlink failed:", exc)
print("profiles", isolated_profile)

overlay = [
    # The user patch already inserted these ids; an overlay that only repeats the
    # id merges instead of replacing, so remove first and insert the isolated copy.
    {"remove": "stage-relay-deepseek"},
    {"remove": "stage-relay-deepseek-native"},
    {"insert": [
        {"id": "stage-relay-deepseek", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "stage-relay-deepseek",
            "transport": "stdio",
            "command": BRIDGE,
            "args": ["--agent", "deepseek", "--port", "4488", "--data-dir", DATA],
        }},
        {"id": "stage-relay-deepseek-native", "name": PLUGIN, "config": {
            "panelUrl": "http://127.0.0.1:4488",
            "agentId": "deepseek",
            "credentialsFile": os.path.join(DATA, "bridge-credentials.json"),
            "runtimeRoot": RUNTIME_ROOT,
        }},
    ]},
]
patch_path = os.path.join(ISOLATED, "isolated.yml")
with open(patch_path, "w", encoding="utf-8") as fh:
    json.dump(overlay, fh, ensure_ascii=False, indent=1)
print("patch", patch_path)

env = dict(os.environ, DSH_HOME=ISOLATED, ELECTRON_RUN_AS_NODE="1")
if "--dump" in sys.argv:
    result = subprocess.run([ELECTRON, DSH_BIN, "--profile", "relay-verify", "--patch", patch_path,
                             "--dump-config"], capture_output=True, text=True, env=env, timeout=180,
                            encoding="utf-8", errors="replace")
    print("exit", result.returncode)
    text = result.stdout + result.stderr
    with open(os.path.join(ISOLATED, "dump.log"), "w", encoding="utf-8") as fh:
        fh.write(text)
    for line in text.splitlines():
        if "4488" in line or "panelUrl" in line:
            print(line.strip()[:200])
    sys.exit(0 if result.returncode == 0 else 1)

# Real boot: an isolated DSH web profile on its own port, with the Stage Relay
# plugin pointed at the candidate instance. The user's desktop app is untouched.
port = os.environ.get("DSH_WEB_PORT", "7799")
command = [ELECTRON, DSH_BIN, "--profile", "relay-verify", "--patch", patch_path, "--",
           "--port", port]
print("boot:", " ".join(command[:6]), "-- --port", port)
log = open(os.path.join(ISOLATED, "boot.log"), "w", encoding="utf-8")
process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
with open(os.path.join(ISOLATED, "boot.pid"), "w") as fh:
    fh.write(str(process.pid))
print("pid", process.pid)
time.sleep(25)
print("alive", process.poll() is None)
sys.exit(0)
