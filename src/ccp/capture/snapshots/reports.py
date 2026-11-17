"""Telling the server about each snapshot commit (see :mod:`ccp.shadow`).

A report is written to the state folder first and removed once the API has it, so a hook that is cut
short, or an API that is down, loses nothing; the next snapshot or session start sends what waits.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from ccp.capture.api import ApiRejectedError, ApiUnavailableError
from ccp.capture.snapshots.git import Git, GitError
from ccp.capture.state import StateStore
from ccp.shadow import MAX_FILES

logger = logging.getLogger("ccp.capture.snapshots")

REPORT_PREFIX = "shadow-report-"
MAX_WAITING = 500  # per session; the oldest are dropped beyond that
_NOT_THERE = (404, 405, 501)  # an older API has no such endpoint: keep the reports, try later


class SnapshotApi(Protocol):
    """The part of the API the reporter uses."""

    def post_snapshot(self, session_id: str, record: dict[str, Any]) -> None:
        """Report one snapshot commit."""


def changed_files(git: Git, parent: str | None, commit: str) -> tuple[list[dict[str, str]], int]:
    """The files a snapshot changed against its parent: ``([{path, state}], total)``."""
    try:
        base = [parent] if parent else ["--root"]
        output = git.run("diff-tree", "--no-commit-id", "--name-status", "-r", "-z", *base, commit)
    except GitError:
        return [], 0
    parts = output.split("\0")
    files = [
        {"path": parts[i + 1], "state": parts[i][:1]}
        for i in range(0, len(parts) - 1, 2)
        if parts[i][:1] in ("A", "M", "D") and parts[i + 1]
    ]
    return files[:MAX_FILES], len(files)


class SnapshotReporter:
    """Keeps snapshot reports until the API has them."""

    def __init__(self, state: StateStore, api: SnapshotApi | None) -> None:
        """Create the reporter; without an ``api`` reports are kept and never sent."""
        self._state = state
        self._api = api

    def queue(self, session_id: str, record: dict[str, Any]) -> None:
        """Keep a report until :meth:`send` delivers it."""
        self._state.set_marker(
            session_id, f"{REPORT_PREFIX}{record['sha'][:16]}", json.dumps(record)
        )
        names = self._state.marker_names(session_id, REPORT_PREFIX)
        for old in names[: max(0, len(names) - MAX_WAITING)]:
            self._state.remove_marker(session_id, old)

    def send(self, session_id: str) -> str | None:
        """Send the session's waiting reports, oldest first; ``None`` if nothing waits."""
        names = self._state.marker_names(session_id, REPORT_PREFIX)
        if not names or self._api is None:
            return None
        sent = 0
        for name in names:
            try:
                record = json.loads(self._state.get_marker(session_id, name) or "")
            except ValueError:
                self._state.remove_marker(session_id, name)
                continue
            try:
                self._api.post_snapshot(session_id, record)
            except ApiUnavailableError:
                return f"{len(names) - sent} snapshot report(s) wait (API unavailable)"
            except ApiRejectedError as error:
                if error.status in _NOT_THERE or error.status in (401, 403):
                    return f"snapshot reports wait (the API answered {error.status})"
                logger.error("snapshot report refused and dropped (%d)", error.status)
            self._state.remove_marker(session_id, name)
            sent += 1
        return f"{sent} snapshot report(s) sent"

    def send_all(self) -> None:
        """Send the waiting reports of every session (at a session start)."""
        for session_id in self._state.sessions_with_markers(REPORT_PREFIX):
            if self.send(session_id) and self._api is None:
                break
