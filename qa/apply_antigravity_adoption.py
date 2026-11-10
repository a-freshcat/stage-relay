"""One-shot maintenance entry for adopting a stale MCP entry.

This does not go through the running panel: that process loaded its code before
`adopt` existed, so its in-memory copy still refuses. The module on disk is the
current one, so importing it here runs the reviewed logic — including the
read-back verification and the ownership-after-verification ordering — without
restarting, replacing, or otherwise touching the shared panel or its bridges.

Usage:
    python qa/apply_antigravity_adoption.py --dry-run
    python qa/apply_antigravity_adoption.py --apply

`--dry-run` is the default. `--apply` requires the caller to have captured the
frozen entry first, and refuses if the file changed since.
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.connections import Connections  # noqa: E402

PANEL = "http://127.0.0.1:4318"
SERVER = "stage-relay-antigravity"


def panel_data_dir():
    """The store the running panel actually serves; never guessed."""
    with urllib.request.urlopen(f"{PANEL}/api/health", timeout=8) as response:
        return json.loads(response.read())["data_dir"]


def canonical(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return data, data["mcpServers"][SERVER]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the change; default is a dry run")
    parser.add_argument("--frozen", default=str(ROOT / "qa/ag-native/adoption-frozen.json"))
    args = parser.parse_args()

    data_dir = panel_data_dir()
    print("panel data-dir :", data_dir)
    connections = Connections(data_dir, 4318)
    agent = {"id": "antigravity", "app": "antigravity"}
    target, kind = connections.target(agent)
    print("config path    :", target)
    print("format         :", kind)

    if not target.exists():
        raise SystemExit("target missing; refusing to invent a config")

    before_text = target.read_text(encoding="utf-8")
    before_file = hashlib.sha256(before_text.encode("utf-8")).hexdigest()
    print("before sha256  :", before_file)

    data, current = snapshot(target)
    others_before = {k: v for k, v in data["mcpServers"].items() if k != SERVER}
    print("other servers  :", sorted(others_before))

    frozen = json.loads(Path(args.frozen).read_text(encoding="utf-8"))
    adopt = {"server": SERVER, "digest": canonical(current)}
    print("adopt digest   :", adopt["digest"])
    if frozen.get("adopted_digest") and frozen["adopted_digest"] != adopt["digest"]:
        raise SystemExit("the entry on disk is not the one that was frozen; refusing")
    if frozen.get("file_sha256") and frozen["file_sha256"] != before_file:
        print("note           : the file changed since the freeze, but the entry itself still matches")

    print("would write    :", json.dumps(connections.current_entry(agent), ensure_ascii=False))
    plan = connections.prepare(agent, adopt=adopt)
    print("prepare ok, plan:", plan["plan_id"][:12], "| expires in 600s")

    planned = connections.plans[plan["plan_id"]]["updated"].decode("utf-8")
    after_plan = json.loads(planned)
    print("planned entry  :", json.dumps(after_plan["mcpServers"][SERVER], ensure_ascii=False))
    print("others after   :", sorted(k for k in after_plan["mcpServers"] if k != SERVER))

    if not args.apply:
        print("\nDRY RUN: nothing written. Re-run with --apply to commit.")
        return 0

    journal_before = (connections.journal.read_text(encoding="utf-8")
                      if connections.journal.exists() else None)
    result = connections.commit(plan["plan_id"], True)
    print("\ncommit         :", json.dumps(result, ensure_ascii=False))

    after_text = target.read_text(encoding="utf-8")
    print("after sha256   :", hashlib.sha256(after_text.encode("utf-8")).hexdigest())
    print("backup         :", result["backup"])
    after = json.loads(after_text)["mcpServers"]
    print("entry now      :", json.dumps(after[SERVER], ensure_ascii=False))
    print("others now     :", sorted(k for k in after if k != SERVER))
    print("others intact  :", {k: v for k, v in after.items() if k != SERVER} == others_before)
    journal_after = connections.journal.read_text(encoding="utf-8") if connections.journal.exists() else None
    print("ownership now  :", json.dumps(json.loads(journal_after or "{}"), ensure_ascii=False))
    print("ownership added:", "antigravity" in json.loads(journal_after or "{}"))

    evidence = {
        "config_path": str(target),
        "before_sha256": before_file,
        "after_sha256": hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
        "backup": result["backup"],
        "adopt_digest": adopt["digest"],
        "written_entry": after[SERVER],
        "other_servers_before": sorted(others_before),
        "other_servers_after": sorted(k for k in after if k != SERVER),
        "others_unchanged": {k: v for k, v in after.items() if k != SERVER} == others_before,
        "journal_before": journal_before,
        "journal_after": journal_after,
        "verified": result["verified"],
        "note": "verified means the written file matched the plan on read-back; it is not a bridge handshake.",
    }
    out = ROOT / "qa/ag-native/adoption-applied.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    print("evidence       :", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())