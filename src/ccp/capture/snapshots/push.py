"""Where snapshots are pushed, and what happens when the push fails.

Snapshots go to a **capture project**: a separate Git repository, one per source repository, that
holds only the hidden snapshot refs. Its address comes from the settings:

* ``CCP_SNAPSHOT_REMOTE``: the project's URL, or
* ``CCP_SNAPSHOT_REMOTE_BASE``: a group URL; the project is ``<base>/<repository name>-capture.git``
  (the name is taken from the ``origin`` URL, or from the folder name if there is no origin).

A push that fails (no network, the project does not exist yet) is written to a small queue file and
retried at the next SessionStart, so a snapshot is never lost for that reason and the developer is
never blocked. The queue holds only the repository and the refs, **not the remote address**: that
address may contain credentials, and it is looked up from the current settings at retry time.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from ccp.capture.snapshots.git import Git, GitError

# Characters allowed in a derived repository name (anything else becomes "-").
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")

# Refs pushed in one command (keeps the command line short for repositories with many branches).
PUSH_BATCH_SIZE = 40


def repository_name(git: Git) -> str:
    """Return the repository's name: from the ``origin`` URL, else from the folder name."""
    try:
        origin = git.run("remote", "get-url", "origin")
    except GitError:
        origin = ""
    name = Path(origin.replace("\\", "/").rstrip("/")).name if origin else git.root.name
    name = name.removesuffix(".git") or git.root.name
    return _UNSAFE_NAME_CHARACTERS.sub("-", name).strip("-") or "repository"


def resolve_remote(git: Git, remote: str | None, remote_base: str | None) -> str | None:
    """Return the capture project to push to, or ``None`` if none is configured (keep local)."""
    if remote:
        return remote
    if remote_base:
        return f"{remote_base.rstrip('/')}/{repository_name(git)}-capture.git"
    return None


class PushQueue:
    """A file of pushes that failed and must be retried (one JSON object per line)."""

    def __init__(self, path: Path) -> None:
        """Use ``path`` as the queue file (created on first use)."""
        self._path = path

    def add(self, root: Path, refspecs: list[str]) -> None:
        """Remember a failed push (the repository and the refs; never the remote address)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"root": str(root), "refspecs": refspecs}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def retry_all(self, push: Callable[[Path, list[str]], bool]) -> int:
        """Try every queued push again; keep the ones that still fail.

        Args:
            push: Called with ``(repository root, refspecs)``; returns True on success.

        Returns:
            How many queued pushes succeeded.
        """
        if not self._path.is_file():
            return 0
        entries = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue  # a damaged line is dropped rather than blocking the queue
        remaining, delivered = [], 0
        for entry in entries:
            root = Path(entry["root"])
            if root.is_dir() and push(root, list(entry["refspecs"])):
                delivered += 1
            elif root.is_dir():  # still failing: keep it (a repository that is gone is dropped)
                remaining.append(entry)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text("".join(json.dumps(e) + "\n" for e in remaining), encoding="utf-8")
        os.replace(temporary, self._path)
        return delivered

    def __len__(self) -> int:
        """Return how many pushes are waiting."""
        if not self._path.is_file():
            return 0
        return sum(
            1 for line in self._path.read_text(encoding="utf-8").splitlines() if line.strip()
        )


def push_refs(git: Git, remote: str, refspecs: list[str], timeout: float) -> bool:
    """Push ``refspecs`` to ``remote`` in batches; return True only if every batch succeeded."""
    for start in range(0, len(refspecs), PUSH_BATCH_SIZE):
        batch = refspecs[start : start + PUSH_BATCH_SIZE]
        try:
            git.run("push", "-q", remote, *batch, timeout=timeout)
        except GitError:
            return False
    return True
