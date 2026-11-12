"""The snapshot actions that hooks call: take a snapshot, push it, back up branches.

:class:`SnapshotService` is what the hook handler uses. Every method is safe to call at any time:

* it does nothing (returns ``None``) if snapshots are not enabled;
* it reports "not a git repository" for a directory outside Git (a session without code);
* it never raises: a Git problem becomes a short reason for the log (no Git output, no paths).

A snapshot is pushed to the capture project (see :mod:`ccp.capture.snapshots.push`); if the push
fails it is queued and retried at the next session start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ccp.capture.snapshots.git import Git, GitError, find_repository_root
from ccp.capture.snapshots.push import PushQueue, push_refs, resolve_remote
from ccp.capture.snapshots.taker import SnapshotResult, SnapshotTaker
from ccp.capture.state import StateStore
from ccp.settings import ClientSettings

logger = logging.getLogger("ccp.capture.snapshots")

QUEUE_FILE_NAME = "pending-pushes.jsonl"
WORK_FOLDER_NAME = "snapshot-work"


class SnapshotService:
    """Takes and delivers snapshots for the hooks."""

    def __init__(
        self,
        state: StateStore,
        state_dir: Path,
        *,
        enabled: bool,
        remote: str | None,
        remote_base: str | None,
        git_timeout_seconds: float = 10.0,
        push_timeout_seconds: float = 8.0,
    ) -> None:
        """Create the service.

        Args:
            state: The client's small local state (snapshot numbers, last tree).
            state_dir: The state folder (the push queue and temporary files live in it).
            enabled: Whether snapshots are taken at all.
            remote: The capture project's URL, if configured.
            remote_base: Or a group URL; the project is ``<base>/<repository>-capture.git``.
            git_timeout_seconds: Time limit for each local Git command.
            push_timeout_seconds: Time limit for each push.
        """
        self._state = state
        self._enabled = enabled
        self._remote = remote
        self._remote_base = remote_base
        self._git_timeout = git_timeout_seconds
        self._push_timeout = push_timeout_seconds
        self._queue = PushQueue(state_dir / QUEUE_FILE_NAME)
        self._work_dir = state_dir / WORK_FOLDER_NAME

    # ------------------------------------------------------------ what hooks call

    def start(self, session_id: str, cwd: Path) -> str | None:
        """At session start: retry queued pushes, take the baseline snapshot, back up branches."""
        return self._guarded(cwd, lambda git: self._start(git, session_id))

    def snapshot(self, session_id: str, cwd: Path, event: str) -> str | None:
        """After a response: snapshot the working tree if it changed, and push it."""
        return self._guarded(cwd, lambda git: self._snapshot_and_push(git, session_id, event))

    def finish(self, session_id: str, cwd: Path) -> str | None:
        """At session end: a final snapshot and a branch backup."""
        return self._guarded(cwd, lambda git: self._finish(git, session_id))

    # ------------------------------------------------------------ steps

    def _guarded(self, cwd: Path, action: Callable[[Git], str]) -> str | None:
        """Run ``action(git)`` for the repository around ``cwd``; every problem becomes text."""
        if not self._enabled:
            return None
        root = find_repository_root(cwd)
        if root is None:
            return "not a git repository"
        try:
            return action(Git(root, self._git_timeout))
        except (GitError, ValueError) as error:
            logger.warning("snapshot step failed: %s", error)
            return f"failed ({error})"

    def _start(self, git: Git, session_id: str) -> str:
        delivered = self._queue.retry_all(self._retry_push)
        parts = [f"{delivered} queued push(es) delivered"] if delivered else []
        parts.append(self._snapshot_and_push(git, session_id, "SessionStart"))
        parts.append(self._backup_branches(git))
        return "; ".join(parts)

    def _retry_push(self, root: Path, refspecs: list[str]) -> bool:
        """Push queued refs again; the remote is looked up now, from the current settings."""
        git = Git(root, self._git_timeout)
        remote = resolve_remote(git, self._remote, self._remote_base)
        return remote is not None and push_refs(git, remote, refspecs, self._push_timeout)

    def _finish(self, git: Git, session_id: str) -> str:
        return "; ".join(
            [self._snapshot_and_push(git, session_id, "SessionEnd"), self._backup_branches(git)]
        )

    def _snapshot_and_push(self, git: Git, session_id: str, event: str) -> str:
        result = SnapshotTaker(git, self._state, self._work_dir).take(session_id, event)
        if result.status != "created" or result.ref is None:
            return f"no snapshot ({result.status}){_excluded_note(result)}"
        description = f"snapshot {result.ref.rsplit('/', 1)[1]}" + _excluded_note(result)
        if result.local_only:
            return description + ", kept local (a secret is in unpushed commits)"
        return f"{description}, {self._deliver(git, [f'{result.ref}:{result.ref}'])}"

    def _backup_branches(self, git: Git) -> str:
        refspecs = SnapshotTaker(git, self._state, self._work_dir).backup_branches()
        if not refspecs:
            return "no branches to back up"
        return f"{len(refspecs)} branch(es) backed up, {self._deliver(git, refspecs)}"

    def _deliver(self, git: Git, refspecs: list[str]) -> str:
        """Push to the capture project, queueing the push if it fails."""
        remote = resolve_remote(git, self._remote, self._remote_base)
        if remote is None:
            return "kept locally (no capture project configured)"
        if push_refs(git, remote, refspecs, self._push_timeout):
            return "pushed"
        self._queue.add(git.root, refspecs)
        return "push failed, queued for the next session start"


def snapshot_service_from(settings: ClientSettings, state: StateStore) -> SnapshotService:
    """Create the snapshot service from the client settings.

    Snapshots are off unless the settings turn them on (see ``CCP_SNAPSHOTS`` and the remote
    settings in docs/configuration.md); a disabled service does nothing.
    """
    return SnapshotService(
        state,
        settings.state_dir,
        enabled=settings.snapshots_enabled,
        remote=settings.snapshot_remote,
        remote_base=settings.snapshot_remote_base,
    )


def _excluded_note(result: SnapshotResult) -> str:
    """Describe excluded files for the log: how many, never their names."""
    return f" ({len(result.excluded)} file(s) excluded: secret)" if result.excluded else ""
