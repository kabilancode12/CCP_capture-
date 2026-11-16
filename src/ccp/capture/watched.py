"""The list of repositories the watcher looks at (written by the snapshot hooks)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("ccp.capture.watch")

REPOS_FILE = "watched-repos.json"


def register_repo(state_dir: Path, root: Path, session_id: str) -> None:
    """Remember a repository (and the chat that last used it) for the watcher."""
    path = state_dir / REPOS_FILE
    try:
        repos: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )
        entry = repos.setdefault(str(root), {})
        if entry.get("session") == session_id:
            return
        entry["session"] = session_id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(repos), encoding="utf-8")
    except (OSError, ValueError):
        logger.warning("could not remember the repository for the watcher")
