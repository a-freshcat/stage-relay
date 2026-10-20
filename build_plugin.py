"""Build a source plugin ZIP without local config, keys, runs or machine wiring."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from relaylib import __version__
from relaylib.runner import ROOT


def build(output):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    names = ["README.md", "CHANGELOG.md", ".gitignore", ".codex-plugin/plugin.json", "relay.py", "panel.py",
             "mcp_server.py", "configure_plugin.py", "build_plugin.py", "demo.py", "audit_ccswitch.py",
             "config.example.json", "config.ccswitch-native.example.json"]
    for folder in ("relaylib", "panel", "skills", "tests", "assets", "docs"):
        names.extend(p.relative_to(ROOT).as_posix() for p in (ROOT / folder).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".md", ".js", ".css", ".html", ".svg"))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            archive.write(ROOT / name, "stage-relay/" + name)
        # Valid unconfigured package. Configure locally before installing/enabling.
        archive.writestr("stage-relay/.mcp.json", '{"mcpServers":{}}\n')
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        members = archive.namelist()
        assert not any(".local." in name or "/runs/" in name or "__pycache__" in name for name in members)
        assert json.loads(archive.read("stage-relay/.mcp.json")) == {"mcpServers": {}}
    return {"zip": str(output), "files": len(members), "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT.parent / "deliverables" / f"stage-relay-v{__version__}-plugin.zip"))
    print(json.dumps(build(parser.parse_args().output)))
