"""Subprocess fixture emulating the Codex CLI contract; never calls a real model."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


args = sys.argv[1:]
if args and args[0] == "heartbeat":
    while True:
        Path(args[1]).write_text(str(time.time()))
        time.sleep(0.05)


def option(name):
    return args[args.index(name) + 1]


prompt = sys.stdin.buffer.read().decode("utf-8")
if "--output-schema" in args:
    raise AssertionError("Relay must not require upstream json_schema support")
sequence_file = Path(os.environ["RELAY_MOCK_SEQUENCE"])
sequence = json.loads(sequence_file.read_text(encoding="utf-8"))
index = sequence["cursor"]
entry = sequence["entries"][index]
sequence["cursor"] = index + 1
sequence_file.write_text(json.dumps(sequence), encoding="utf-8")
if entry.get("sleep"):
    if entry.get("heartbeat"):
        subprocess.Popen([sys.executable, __file__, "heartbeat", entry["heartbeat"]])
    time.sleep(entry["sleep"])
config = {}
for index, arg in enumerate(args):
    if arg == "-c":
        key, value = args[index + 1].split("=", 1)
        try:
            config[key] = json.loads(value)
        except ValueError:
            config[key] = value
base_url = config["model_providers.stage_relay.base_url"]
headers = {"Authorization": "Bearer " + os.environ["STAGE_RELAY_TOKEN"], "Content-Type": "application/json"}
body = json.dumps({"model": entry.get("request_model", option("-m")), "input": prompt,
                   "fixture_sandbox": option("-s")}).encode()
for _ in range(entry.get("requests", 1)):
    request = urllib.request.Request(base_url + "/responses", data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except urllib.error.HTTPError as error:
        error.close()
        print(json.dumps({"type": "turn.failed", "error": "fixture upstream failure"}))
        sys.exit(3)
    except (urllib.error.URLError, OSError):
        print(json.dumps({"type": "turn.failed", "error": "fixture upstream failure"}))
        sys.exit(3)
if entry.get("exit_code"):
    sys.exit(entry["exit_code"])
if entry.get("overflow"):
    print("x" * entry["overflow"])
else:
    print("WARNING: fixture warning on stdout")
    print(json.dumps({"type": "thread.started", "thread_id": "mock-thread"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10}}))
for name, value in entry.get("write", {}).items():
    (Path(option("-C")) / name).write_text(value, encoding="utf-8")
output = Path(option("-o"))
if entry.get("invalid_json"):
    output.write_text("NOT JSON")
else:
    output.write_text(json.dumps(entry["response"]), encoding="utf-8")
