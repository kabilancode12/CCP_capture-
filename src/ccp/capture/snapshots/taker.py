"""Take a snapshot of the working tree without touching the developer's repository state.

A snapshot is a hidden Git commit holding the files **as they are on disk right now**, including
changes the developer has not committed and files that are not tracked yet. It is built with a
temporary index file, so the developer's own index (their staged changes), HEAD, branches and
files are never changed. Only a hidden ref is added::

    refs/claude-capture/sessions/<session_id>/<seq>       one per snapshot, numbered 0001, 0002, ...
    refs/claude-capture/users/<user>/heads/<branch>       a backup of each local branch tip

How a snapshot is made, in order:

1. Copy HEAD into a temporary index and add every working-tree file to it (``.gitignore`` is
   respected, so ignored files such as ``.env`` are not included).
2. **Exclude files that contain a secret** (same secret rules as for transcripts): such a file is
   reset to its committed version, or left out if it is new, and its name is recorded in the commit
   message.
3. Write the tree. If it equals the last snapshot of this session, or (for the first snapshot) the
   committed tree, there is nothing to record.
4. If the developer has local commits that are not pushed anywhere, scan them for secrets too:
   a snapshot is pushed together with the commits it builds on, so a secret there keeps the snapshot
   local.
5. Create a commit on top of HEAD with the tree, and point the hidden ref at it.

Known limits: snapshots are per response, not per edit; every file is hashed on each snapshot (a
very large repository makes it slower); text files over 20 MB are excluded as unscannable.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ccp.capture.snapshots.context import HUMAN, SnapshotContext
from ccp.capture.snapshots.git import Git, GitError
from ccp.capture.state import StateStore
from ccp.redaction.redactor import redact_string
from ccp.redaction.rules import SNAPSHOT_SECRET_RULES
from ccp.storage.keys import check_id

REF_NAMESPACE = "refs/claude-capture"
CHAIN_NAMESPACE = f"{REF_NAMESPACE}/chain"  # one growing history per developer, machine and branch
PUSHED_BRANCH_PREFIX = "refs/heads/ccp"  # where the chain is published in the capture project
LOCK_STALE_SECONDS = 60.0
LOCK_WAIT_SECONDS = 8.0

# Text files larger than this are not scanned, so they are excluded (fail closed).
SECRET_SCAN_LIMIT_BYTES = 20 * 1024 * 1024
# The most of the unpushed-commit patch that is scanned.
PATCH_SCAN_LIMIT_BYTES = 20 * 1024 * 1024

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_SLUG_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")

SEQ_MARKER = "snapshot-seq"
LAST_TREE_MARKER = "snapshot-last-tree"
SCANNED_HEAD_MARKER = "snapshot-scanned-head"


@dataclass(frozen=True)
class SnapshotResult:
    """The outcome of :meth:`SnapshotTaker.take`.

    Attributes:
        status: ``created``, ``unchanged`` (the tree equals the last snapshot) or ``no-changes``
            (nothing differs from the committed state, at the start of a session).
        ref: The hidden ref of the new snapshot (only when ``created``).
        commit: The snapshot commit (only when ``created``).
        excluded: Files left out because they contain a secret.
        local_only: True if a secret was found in unpushed commits: the snapshot must not be pushed.
    """

    status: str
    ref: str | None = None
    commit: str | None = None
    excluded: tuple[str, ...] = ()
    local_only: bool = False
    chain_ref: str | None = None
    published_ref: str | None = None
    parent: str | None = None
    tree: str | None = None


def user_slug(git: Git) -> str:
    """Return a name for the developer that is safe in a ref (from their Git e-mail address)."""
    email = git.identity()[1] or os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    return _UNSAFE_SLUG_CHARACTERS.sub("_", email)


def _safe_name(name: str) -> str:
    """Make a file name safe to put in a commit message line (no control characters)."""
    return _CONTROL_CHARACTERS.sub("?", name)


def host_slug() -> str:
    """The machine's name made safe for a ref."""
    return _UNSAFE_SLUG_CHARACTERS.sub("_", socket.gethostname() or "host")


def _ref_part(text: str) -> str:
    """A branch name as one safe ref component (slashes are kept as ``__``)."""
    return _UNSAFE_SLUG_CHARACTERS.sub("_", text.replace("/", "__")).strip(".") or "detached"


class _RepoLock:
    """A lock file so two hooks (parallel tool calls, sub-agents) never use the index together."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> _RepoLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                os.close(os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self._path.stat().st_mtime > LOCK_STALE_SECONDS:
                        self._path.unlink(missing_ok=True)  # left by a killed hook
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError("the snapshot lock is held by another hook") from None
                time.sleep(0.05)

    def __exit__(self, *_: object) -> None:
        self._path.unlink(missing_ok=True)


def file_has_secret(path: Path) -> bool:
    """Return True if the file holds something the secret rules match.

    Binary files (containing a NUL byte) are not scanned: they are not text a rule could read.
    A text file too large to scan counts as containing a secret (fail closed).
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(SECRET_SCAN_LIMIT_BYTES + 1)
    except OSError:
        return False  # deleted or unreadable while we looked: nothing to scan
    if b"\x00" in data[:8000]:
        return False
    if len(data) > SECRET_SCAN_LIMIT_BYTES:
        return True
    _, fired = redact_string(data.decode("utf-8", errors="replace"), SNAPSHOT_SECRET_RULES)
    return bool(fired)


class SnapshotTaker:
    """Takes snapshots of one repository for one session at a time."""

    def __init__(self, git: Git, state: StateStore, work_dir: Path) -> None:
        """Create the taker.

        Args:
            git: The repository to snapshot.
            state: Remembers each session's last snapshot number and tree.
            work_dir: A folder for temporary index files (outside the repository).
        """
        self._git = git
        self._state = state
        self._work_dir = work_dir

    def take(
        self, session_id: str, event: str, context: SnapshotContext | None = None
    ) -> SnapshotResult:
        """Snapshot the working tree if it changed.

        Without a ``context`` the snapshot is a hidden per-session ref on top of HEAD (the first
        form of this module). With one it is added to the **capture chain** of this developer,
        machine and branch: a linear history in which every commit is one change, labelled with who
        made it (see :class:`~ccp.capture.snapshots.context.SnapshotContext`).

        Args:
            session_id: Claude Code's session id.
            event: The hook event that triggered it (recorded in the commit message).
            context: Who or what made the change; ``None`` for the per-session form.

        Raises:
            GitError: If a Git command fails.
            ValueError: If ``session_id`` is not a safe identifier.
        """
        check_id("session id", session_id)
        if context is not None:
            return self._take_chained(session_id, event, context)
        head = self._git.head()
        tree, excluded = self._build_tree(head)

        last_tree = self._state.get_marker(session_id, LAST_TREE_MARKER)
        if last_tree == tree:
            return SnapshotResult("unchanged", excluded=excluded)
        if last_tree is None and head and self._git.run("rev-parse", f"{head}^{{tree}}") == tree:
            return SnapshotResult("no-changes", excluded=excluded)

        local_only = self._unpushed_commits_have_secret(session_id, head)
        sequence = int(self._state.get_marker(session_id, SEQ_MARKER) or 0) + 1
        commit = self._commit(tree, head, session_id, sequence, event, excluded)
        ref = f"{REF_NAMESPACE}/sessions/{session_id}/{sequence:04d}"
        self._git.run("update-ref", ref, commit)
        self._state.set_marker(session_id, SEQ_MARKER, str(sequence))
        self._state.set_marker(session_id, LAST_TREE_MARKER, tree)
        return SnapshotResult("created", ref, commit, excluded, local_only)

    def chain_ref(self) -> str:
        """The local ref of this developer's capture chain for the current branch."""
        parts = (user_slug(self._git), host_slug(), _ref_part(self._git.branch()))
        return f"{CHAIN_NAMESPACE}/{'/'.join(parts)}"

    def published_ref(self) -> str:
        """The chain's branch name in the capture project: ``ccp/<user>/<host>/<branch>``."""
        return self.chain_ref().replace(CHAIN_NAMESPACE, PUSHED_BRANCH_PREFIX, 1)

    def _take_chained(
        self, session_id: str, event: str, context: SnapshotContext
    ) -> SnapshotResult:
        """Add one commit to the capture chain if the working tree differs from its tip."""
        key = hashlib.sha1(str(self._git.root).encode()).hexdigest()[:12]  # noqa: S324
        with _RepoLock(self._work_dir / f"lock-{key}"):
            head = self._git.head()
            tree, excluded = self._build_tree(head, persistent=True)
            ref = self.chain_ref()
            try:
                tip: str | None = self._git.run("rev-parse", "-q", "--verify", ref)
            except GitError:
                tip = None
            if tip is not None:
                if self._git.run("rev-parse", f"{tip}^{{tree}}") == tree:
                    return SnapshotResult("unchanged", excluded=excluded, chain_ref=ref)
            elif head and self._git.run("rev-parse", f"{head}^{{tree}}") == tree:
                return SnapshotResult("no-changes", excluded=excluded, chain_ref=ref)
            local_only = self._unpushed_commits_have_secret(session_id, head)
            sequence = int(self._state.get_marker(session_id, SEQ_MARKER) or 0) + 1
            parent = tip or head
            commit = self._commit(
                tree, parent, session_id, sequence, event, excluded, context, head
            )
            self._git.run("update-ref", ref, commit)
            self._state.set_marker(session_id, SEQ_MARKER, str(sequence))
            self._state.set_marker(session_id, LAST_TREE_MARKER, tree)
            return SnapshotResult(
                "created",
                ref,
                commit,
                excluded,
                local_only,
                ref,
                self.published_ref(),
                parent,
                tree,
            )

    def backup_branches(self) -> list[str]:
        """Point a hidden ref at the tip of every local branch; return the refspecs to push.

        The refs live under ``refs/claude-capture/users/<user>/heads/`` and keep only the latest
        tip of each branch.
        """
        namespace = f"{REF_NAMESPACE}/users/{user_slug(self._git)}/heads"
        listing = self._git.run("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/")
        updates, refspecs = [], []
        for line in listing.splitlines():
            sha, _, full_name = line.partition(" ")
            target = f"{namespace}/{full_name.removeprefix('refs/heads/')}"
            updates.append(f"update {target} {sha}\n")
            refspecs.append(f"+{target}:{target}")  # a branch tip moves, so force the backup
        if updates:
            self._git.run("update-ref", "--stdin", input_text="".join(updates))
        return refspecs

    # ------------------------------------------------------------ steps

    def _build_tree(
        self, head: str | None, persistent: bool = False
    ) -> tuple[str, tuple[str, ...]]:
        """Build the snapshot tree in an index file; return it and the excluded files.

        A temporary index is used once and removed. A *persistent* one (kept in the work folder,
        per repository) remembers file times, so only changed files are read again: this is what
        keeps a snapshot after every tool call fast. It is only a cache; the developer's own
        index is never used or changed.
        """
        self._work_dir.mkdir(parents=True, exist_ok=True)
        if persistent:
            name = hashlib.sha1(str(self._git.root).encode()).hexdigest()[:12]  # noqa: S324
            index = self._work_dir / f"index-{name}"
            seed = not index.exists()
        else:
            handle, index_name = tempfile.mkstemp(prefix="index-", dir=self._work_dir)
            os.close(handle)
            index = Path(index_name)
            index.unlink()  # git wants to create the index file itself
            seed = True
        env = {"GIT_INDEX_FILE": str(index)}  # the developer's own index is never used or changed
        try:
            if seed:
                if head:
                    self._git.run("read-tree", head, env=env)
                else:
                    self._git.run("read-tree", "--empty", env=env)
            self._git.run("add", "-A", ".", env=env)  # respects .gitignore
            excluded = self._exclude_secrets(env, head)
            return self._git.run("write-tree", env=env), excluded
        finally:
            if not persistent:
                index.unlink(missing_ok=True)

    def _exclude_secrets(self, env: dict[str, str], head: str | None) -> tuple[str, ...]:
        """Take files with secrets out of the temporary index; return their names."""
        base = head or self._git.run("hash-object", "-t", "tree", "--stdin", input_text="")
        changed = self._git.run(
            "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", base, env=env
        )
        with_secrets = [
            name
            for name in (n for n in changed.split("\0") if n)
            if file_has_secret(self._git.root / name)
        ]
        if with_secrets:
            # One Git call for all of them (starting a process is slow on Windows). The names go in
            # on standard input, NUL-separated, and are taken literally (no wildcard or pathspec
            # magic), so an odd file name cannot select other files.
            names = "\0".join(with_secrets) + "\0"
            literal = {**env, "GIT_LITERAL_PATHSPECS": "1"}
            options = ["--pathspec-from-file=-", "--pathspec-file-nul"]
            if head:  # back to the committed version
                self._git.run("reset", "-q", head, *options, env=literal, input_text=names)
            else:  # a new file in an empty repository: leave it out
                self._git.run("rm", "-q", "--cached", *options, env=literal, input_text=names)
        return tuple(_safe_name(name) for name in with_secrets)

    def _unpushed_commits_have_secret(self, session_id: str, head: str | None) -> bool:
        """Scan local commits that exist nowhere else; True if one holds a secret.

        A snapshot is pushed together with the commits it builds on, so a secret in an unpushed
        commit would leave the machine with it. The scan runs again only when HEAD has moved.
        """
        if head is None or self._state.get_marker(session_id, SCANNED_HEAD_MARKER) == head:
            return False
        patch = self._git.run("log", "-p", "--format=", head, "--not", "--remotes", timeout=30)
        _, fired = redact_string(patch[:PATCH_SCAN_LIMIT_BYTES], SNAPSHOT_SECRET_RULES)
        if fired:
            return True
        self._state.set_marker(session_id, SCANNED_HEAD_MARKER, head)
        return False

    def _commit(
        self,
        tree: str,
        parent: str | None,
        session_id: str,
        sequence: int,
        event: str,
        excluded: tuple[str, ...],
        context: SnapshotContext | None = None,
        head: str | None = None,
    ) -> str:
        """Create the snapshot commit on top of ``parent`` (a root commit if there is none)."""
        started_from = head if context is not None else parent
        captured_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        lines = [
            "claude-capture snapshot" if context is None else _subject(context),
            "",
        ]
        if context is not None and context.prompt_excerpt:
            lines += [f"Why: {_safe_name(context.prompt_excerpt)}", ""]
        lines += [
            f"session: {session_id}",
            f"seq: {sequence}",
            f"event: {_safe_name(event)}",
            f"branch: {_safe_name(self._git.branch())}",
            f"started-from: {started_from or 'none'}",
            f"captured-at: {captured_at}",
            f"excluded: {', '.join(excluded) if excluded else 'none'}",
            "",
        ]
        # The developer's own identity if they have one; otherwise a fixed one, so it never fails.
        configured_name, configured_email = self._git.identity()
        name = configured_name or "claude-capture"
        email = configured_email or "capture@localhost"
        author = name
        if context is not None:
            lines += _trailers(context, name, session_id, event)
            if (
                context.actor != HUMAN
            ):  # the author says "AI", on the developer's own e-mail address
                author = f"Claude ({context.model or 'AI'}) for {name}"
        env = {
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        parents = ["-p", parent] if parent else []
        return self._git.run("commit-tree", tree, *parents, input_text="\n".join(lines), env=env)


def _subject(context: SnapshotContext) -> str:
    """The first line of a capture commit: who, and what."""
    who = "Human" if context.actor == HUMAN else f"AI · {context.model or 'unknown model'}"
    if context.actor == HUMAN:
        what = "Changes made outside Claude"
    else:
        what = " ".join(p for p in (context.tool or "Changes", context.target) if p)
    return _safe_name(f"[{who}] {what}")[:200]


def _trailers(context: SnapshotContext, developer: str, session_id: str, event: str) -> list[str]:
    """The machine-readable ``CCP-*`` lines at the end of a capture commit."""
    fields = [
        ("CCP-Actor", context.actor), ("CCP-Developer", developer), ("CCP-Model", context.model),
        ("CCP-Session", session_id), ("CCP-Prompt", context.prompt_id), ("CCP-Tool", context.tool),
        ("CCP-Tool-Use", context.tool_use_id), ("CCP-Agent", context.agent), ("CCP-Event", event),
    ]  # fmt: skip
    return [f"{key}: {_safe_name(str(value))}" for key, value in fields if value]
