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
from datetime import UTC, datetime
from pathlib import Path

from ccp.capture.snapshots.context import SnapshotContext
from ccp.capture.snapshots.git import Git, GitError, find_repository_root
from ccp.capture.snapshots.push import PushQueue, push_refs, resolve_remote
from ccp.capture.snapshots.reports import SnapshotApi, SnapshotReporter, changed_files
from ccp.capture.snapshots.taker import SEQ_MARKER, SnapshotResult, SnapshotTaker
from ccp.capture.state import StateStore
from ccp.capture.watched import register_repo
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
        reporter: SnapshotReporter | None = None,
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
            reporter: Tells the server about each snapshot commit (``None``: not reported).
        """
        self._state = state
        self._enabled = enabled
        self._remote = remote
        self._remote_base = remote_base
        self._git_timeout = git_timeout_seconds
        self._push_timeout = push_timeout_seconds
        self._queue = PushQueue(state_dir / QUEUE_FILE_NAME)
        self._work_dir = state_dir / WORK_FOLDER_NAME
        self._state_dir = state_dir
        self._reporter = reporter

    # ------------------------------------------------------------ what hooks call

    def start(
        self, session_id: str, cwd: Path, context: SnapshotContext | None = None
    ) -> str | None:
        """At session start: retry queued pushes, take the baseline snapshot, back up branches."""
        return self._guarded(cwd, lambda git: self._start(git, session_id, context))

    def snapshot(
        self, session_id: str, cwd: Path, event: str, context: SnapshotContext | None = None
    ) -> str | None:
        """Snapshot the working tree if it changed, and push it (see :meth:`SnapshotTaker.take`)."""
        return self._guarded(
            cwd, lambda git: self._snapshot_and_push(git, session_id, event, context)
        )

    def backup(self, cwd: Path) -> str | None:
        """Back up every local branch (unpushed commits included) and push the capture chain."""
        return self._guarded(cwd, self._publish)

    def note_commit(self, cwd: Path) -> str | None:
        """After a commit: write a Git note linking it to the capture chain's newest commit."""
        return self._guarded(cwd, self._note_commit)

    def finish(
        self, session_id: str, cwd: Path, context: SnapshotContext | None = None
    ) -> str | None:
        """At session end: a final snapshot and a branch backup."""
        return self._guarded(cwd, lambda git: self._finish(git, session_id, context))

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

    def _start(self, git: Git, session_id: str, context: SnapshotContext | None) -> str:
        delivered = self._queue.retry_all(self._retry_push)
        parts = [f"{delivered} queued push(es) delivered"] if delivered else []
        if self._reporter is not None:
            self._reporter.send_all()
        parts.append(self._snapshot_and_push(git, session_id, "SessionStart", context))
        parts.append(self._backup_branches(git))
        return "; ".join(parts)

    def _retry_push(self, root: Path, refspecs: list[str]) -> bool:
        """Push queued refs again; the remote is looked up now, from the current settings."""
        git = Git(root, self._git_timeout)
        remote = resolve_remote(git, self._remote, self._remote_base)
        return remote is not None and push_refs(git, remote, refspecs, self._push_timeout)

    def _finish(self, git: Git, session_id: str, context: SnapshotContext | None) -> str:
        return "; ".join(
            [
                self._snapshot_and_push(git, session_id, "SessionEnd", context),
                self._backup_branches(git),
            ]
        )

    def _publish(self, git: Git) -> str:
        taker = SnapshotTaker(git, self._state, self._work_dir)
        parts = [self._backup_branches(git)]
        chain = taker.chain_ref()
        if git.succeeds("rev-parse", "-q", "--verify", chain):
            parts.append("chain " + self._deliver(git, [f"+{chain}:{taker.published_ref()}"]))
        return "; ".join(parts)

    def _note_commit(self, git: Git) -> str:
        taker = SnapshotTaker(git, self._state, self._work_dir)
        try:
            tip = git.run("rev-parse", "-q", "--verify", taker.chain_ref())
        except GitError:
            return "no capture chain yet"
        text = f"CCP-Capture-Chain: {taker.published_ref()}\nCCP-Capture-Tip: {tip}"
        git.run("notes", "--ref=ccp", "add", "-f", "-m", text, "HEAD")
        return f"note added (capture tip {tip[:8]})"

    def _snapshot_and_push(
        self, git: Git, session_id: str, event: str, context: SnapshotContext | None = None
    ) -> str:
        register_repo(self._state_dir, git.root, session_id)
        result = SnapshotTaker(git, self._state, self._work_dir).take(session_id, event, context)
        if result.status != "created" or result.ref is None or result.commit is None:
            return f"no snapshot ({result.status}){_excluded_note(result)}"
        name = result.commit[:8] if result.chain_ref else result.ref.rsplit("/", 1)[1]
        description = f"snapshot {name}" + _excluded_note(result)
        if context is not None:
            self._report(git, session_id, event, context, result)
        if result.local_only:
            return description + ", kept local (a secret is in unpushed commits)"
        if result.chain_ref and result.published_ref:
            refspec = f"+{result.chain_ref}:{result.published_ref}"
        else:
            refspec = f"{result.ref}:{result.ref}"
        return f"{description}, {self._deliver(git, [refspec])}"

    def _report(
        self,
        git: Git,
        session_id: str,
        event: str,
        context: SnapshotContext,
        result: SnapshotResult,
    ) -> None:
        """Queue the report of a new snapshot commit and try to send it (never raises)."""
        if self._reporter is None or result.commit is None:
            return
        files, total = changed_files(git, result.parent, result.commit)
        chain = (result.published_ref or "").removeprefix("refs/heads/") or None
        _, email = git.identity()
        record = {
            "sha": result.commit, "parent_sha": result.parent, "actor": context.actor,
            "captured_at": datetime.now(UTC).isoformat(), "files": files, "files_total": total,
            "seq": int(self._state.get_marker(session_id, SEQ_MARKER) or 0), "event": event,
            "branch": git.branch(), "chain": chain, "developer": email,
            "model": context.model, "tool": context.tool, "target": context.target,
            "tool_use_id": context.tool_use_id, "prompt_uuid": context.prompt_id,
            "prompt_excerpt": context.prompt_excerpt, "agent": context.agent,
        }  # fmt: skip
        self._reporter.queue(session_id, {k: v for k, v in record.items() if v is not None})
        self._reporter.send(session_id)

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


def snapshot_service_from(
    settings: ClientSettings, state: StateStore, api: SnapshotApi | None = None
) -> SnapshotService:
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
        reporter=SnapshotReporter(state, api),
    )


def _excluded_note(result: SnapshotResult) -> str:
    """Describe excluded files for the log: how many, never their names."""
    return f" ({len(result.excluded)} file(s) excluded: secret)" if result.excluded else ""
