"""Generate local MCP wiring. No client settings changes and no model requests."""
import argparse
import json
import sys
from pathlib import Path

from relaylib.contracts import RelayError
from relaylib.runner import ROOT, read_config
from relaylib.runtime import atomic_json


def configure(config, projects, output=None):
    config = Path(config).resolve()
    read_config(config)  # Structural validation only; no credential resolution/network.
    args = [str(ROOT / "mcp_server.py"), "--config", str(config),
            "--runs-root", str(ROOT / "runs" / "plugin")]
    for project in projects:
        root = Path(project).resolve()
        if not root.is_dir():
            raise ValueError("Allowed project roots must already exist")
        args += ["--projects-root", str(root)]
    if not projects:
        raise ValueError("At least one project root is required")
    target = Path(output).resolve() if output else ROOT / ".mcp.json"
    atomic_json(target, {"mcpServers": {"stage-relay": {"command": sys.executable, "args": args}}})
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.json"))
    parser.add_argument("--projects-root", action="append", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        target = configure(args.config, args.projects_root, args.output)
        print(json.dumps({"mcp_config": str(target), "model_calls": 0, "client_settings_changed": False}))
    except (RelayError, OSError, ValueError):
        print("Local configuration is invalid; check config and project roots.", file=sys.stderr)
        sys.exit(2)
