"""The background watcher: catch work that no hook sees.

Hooks capture what Claude does and what changed between two prompts. A developer can also edit files
while Claude is closed, or commit without pushing. The watcher runs on the developer's machine,
looks at the repositories the hooks have seen, and:

* after a folder has been **quiet** for a while (60 s by default) with changes that no snapshot has
  recorded, takes a snapshot labelled as made by a person;
* keeps the branches, including **unpushed commits**, backed up in the capture project.

It leaves a repository alone while a Claude tool call is running there (the PreToolUse hook sets a
marker), so Claude's own changes are never recorded as a person's. Everything is best-effort and
silent: an error becomes a short note, never an exception.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ccp.capture.hooks import TOOL_BUSY_MARKER
from ccp.capture.snapshots.context import HUMAN, SnapshotContext
from ccp.capture.snapshots.git import Git, GitError
from ccp.capture.snapshots.service import SnapshotService
from ccp.capture.state import StateStore
from ccp.capture.watched import REPOS_FILE

logger = logging.getLogger("ccp.capture.watch")

DEFAULT_QUIET_SECONDS = 60.0
DEFAULT_BUSY_SECONDS = 600.0
BACKUP_EVERY_SECONDS = 300.0


def signature(git: Git) -> str:
    """A fingerprint of the changed and untracked files (names, sizes, times); empty if none."""
    try:
        listing = git.run("ls-files", "-m", "-o", "--exclude-standard", "-z")
    except GitError:
        return ""
    digest = hashlib.sha1(usedforsecurity=False)
    for name in sorted(n for n in listing.split("\0") if n):
        try:
            stat = (git.root / name).stat()
        except OSError:
            digest.update(f"{name}:gone".encode())
            continue
        digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest() if listing else ""


class Watcher:
    """Looks at the remembered repositories; :meth:`tick` is one look."""

    def __init__(
        self,
        service: SnapshotService,
        state: StateStore,
        state_dir: Path,
        *,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        busy_seconds: float = DEFAULT_BUSY_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the watcher over the snapshot service and the client's state folder."""
        self._service, self._state, self._state_dir = service, state, state_dir
        self._quiet, self._busy, self._clock = quiet_seconds, busy_seconds, clock
        self._seen: dict[str, dict[str, Any]] = {}

    def _tool_running(self) -> bool:
        """True if a Claude tool call started recently and has not finished."""
        for session in self._state.sessions_with_markers(TOOL_BUSY_MARKER):
            started = self._state.get_marker(session, TOOL_BUSY_MARKER)
            try:
                if started is not None and self._clock() - float(started) < self._busy:
                    return True
            except ValueError:
                continue
        return False

    def tick(self) -> list[str]:
        """Look at every remembered repository once; return a note for each thing done."""
        path = self._state_dir / REPOS_FILE
        try:
            repos: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if self._tool_running():
            return ["a Claude tool call is running: waiting"]
        notes: list[str] = []
        for root_text, entry in repos.items():
            root = Path(root_text)
            if not root.is_dir() or not isinstance(entry, dict) or not entry.get("session"):
                continue
            notes += self._look(root, str(entry["session"]))
        return notes

    def _look(self, root: Path, session_id: str) -> list[str]:
        now = self._clock()
        state = self._seen.setdefault(
            root.as_posix(), {"sig": None, "since": now, "taken": "", "backup": 0.0}
        )
        current = signature(Git(root, 10.0))
        if current != state["sig"]:
            state["sig"], state["since"] = current, now  # still changing: wait until it is quiet
            return []
        notes: list[str] = []
        if current and current != state["taken"] and now - state["since"] >= self._quiet:
            note = self._service.snapshot(session_id, root, "Watcher", SnapshotContext(HUMAN))
            state["taken"] = current
            notes.append(f"{root.name}: {note}")
        if now - state["backup"] >= BACKUP_EVERY_SECONDS:
            note = self._service.backup(root)
            state["backup"] = now
            if note:
                notes.append(f"{root.name}: {note}")
        return notes


def run(watcher: Watcher, interval_seconds: float, *, once: bool = False) -> None:
    """Look every ``interval_seconds`` until stopped (or once)."""
    while True:
        for note in watcher.tick():
            logger.info("%s", note)
        if once:
            return
        time.sleep(interval_seconds)


def default_state_dir() -> Path:
    """Where the client keeps its state (``CCP_STATE_DIR`` or ``~/.ccp/capture``)."""
    return Path(os.environ.get("CCP_STATE_DIR") or Path.home() / ".ccp" / "capture")
