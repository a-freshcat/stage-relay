"""Single answer to "which data directory is this build using?".

Two launches of Stage Relay used to resolve different directories: the packaged
exe looked for a release data directory, while `python app.py` always used
`<repo>/data`. A browser pointed at the shared port could then land on a store
that only held the demo project, so real projects "disappeared" without any
data being deleted. This module makes every entry point agree on one answer and
exposes it so the panel can show the path it actually loaded.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

DATA_DIR_ENV = "STAGE_RELAY_DATA"
DATABASE_NAME = "workspace.sqlite3"


def application_dir() -> Path:
    """Directory holding the executable, or the repository for source runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return repository_dir()


def repository_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def release_root(app_dir: Path | None = None) -> Path | None:
    """The `release` directory that contains per-version builds, if any."""
    app = app_dir or application_dir()
    above = app.parent.parent
    if above.name == "release":
        return above
    inside = app / "release"
    if inside.is_dir():
        return inside
    return None


def store_candidates(app_dir: Path | None = None) -> list[Path]:
    """Every data directory this installation could reasonably be using."""
    app = app_dir or application_dir()
    candidates = [app / "data"]
    root = release_root(app)
    if root:
        for version in sorted(root.glob("StageRelay-*")):
            candidates.append(version / "Stage Relay" / "data")
    seen: dict[Path, None] = {}
    for candidate in candidates:
        seen.setdefault(candidate, None)
    return list(seen)


def summarize(directory: Path) -> dict:
    """What a candidate directory actually holds, without trusting mtime alone."""
    database = directory / DATABASE_NAME
    info = {"path": directory, "exists": database.is_file(), "projects": 0, "real_projects": 0, "mtime": 0.0}
    if not info["exists"]:
        return info
    info["mtime"] = database.stat().st_mtime
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute("select body from workspace where id=1").fetchone()
        finally:
            connection.close()
        projects = json.loads(row[0]).get("projects") or [] if row else []
    except (sqlite3.Error, ValueError, TypeError):
        return info
    info["projects"] = len(projects)
    info["real_projects"] = sum(1 for project in projects if not project.get("demo"))
    return info


def _score(info: dict) -> tuple:
    return (1 if info["real_projects"] else 0, info["real_projects"], info["projects"], info["mtime"])


def default_data_dir(app_dir: Path | None = None) -> Path:
    """Pick the data directory with the most real work, not simply the newest file.

    A stray `data/` created by one `python app.py` run must never outrank the
    release store that holds the user's projects, so scoring prefers non-demo
    content first and only then the newer database.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    candidates = store_candidates(app_dir)
    existing = [summarize(candidate) for candidate in candidates if candidate.is_dir() or (candidate / DATABASE_NAME).is_file()]
    chosen = max(existing, key=lambda info: (_score(info), str(info["path"]))) if existing else None
    if chosen and chosen["exists"]:
        return chosen["path"]
    app = app_dir or application_dir()
    return app / "data"


def describe(directory: Path) -> str:
    """Human line for logs and the panel, so a wrong store is obvious."""
    info = summarize(directory)
    return f"{info['path']} (projects={info['projects']}, real={info['real_projects']})"