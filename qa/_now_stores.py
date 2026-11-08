import sys, json
sys.path.insert(0, ".")
from relay.storage import summarize
from pathlib import Path
for p in [Path("data"), Path(r"release\StageRelay-0.3.1-codex-dsh\Stage Relay\data")]:
    i = summarize(p)
    print(f"{str(i['path']):85} projects={i['projects']} real={i['real_projects']}")
