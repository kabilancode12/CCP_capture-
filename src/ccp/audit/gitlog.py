"""Reading commits from a Git repository (the capture project, or any clone of it).

Only what commit linking needs: the commits on some references, and the lines each one adds. Git is
run with a fixed argument list (never through a shell), without prompts, and its text output is read
as UTF-8 with undecodable bytes replaced, so odd file content cannot stop a run.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The branch backups the capture client pushes to the capture project (see docs/snapshots.md).
DEFAULT_REFS = ("refs/claude-capture/users/*/heads/*",)
TIMEOUT_SECONDS = 120


class GitError(RuntimeError):
    """Raised when Git cannot be run or reports an error. The message holds no file content."""


@dataclass(frozen=True)
class Commit:
    """A commit that is not a merge."""

    sha: str
    committed_at: datetime


def _git(repository: Path, *arguments: str) -> str:
    """Run ``git -C <repository> ...`` and return its standard output as text."""
    command = ["git", "-C", str(repository), "-c", "core.quotepath=false", *arguments]
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(  # noqa: S603  (fixed argument list, no shell)
            command,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f"cannot run git: {type(error).__name__}") from error
    if done.returncode != 0:
        first = done.stderr.decode("utf-8", errors="replace").strip().splitlines()[:1]
        raise GitError(f"git {arguments[0]} failed: {first[0] if first else 'no message'}")
    return done.stdout.decode("utf-8", errors="replace")


def list_commits(repository: Path, refs: tuple[str, ...] = DEFAULT_REFS) -> list[Commit]:
    """The commits reachable from the references, newest first, merges left out.

    Args:
        repository: A Git directory or working tree.
        refs: Reference patterns (``--glob`` patterns). An empty tuple means every reference.

    Raises:
        GitError: If Git fails.
    """
    selection = [f"--glob={ref}" for ref in refs] if refs else ["--all"]
    output = _git(repository, "log", "--no-merges", "--format=%H %ct", *selection)
    commits = []
    for line in output.splitlines():
        sha, _, seconds = line.partition(" ")
        if sha and seconds.isdigit():
            commits.append(Commit(sha, datetime.fromtimestamp(int(seconds), tz=UTC)))
    return commits


def added_lines(repository: Path, sha: str) -> dict[str, list[str]]:
    """The lines a commit adds, per file path (the new path of a renamed file).

    Binary files add no lines. The commit's first parent is the base; a root commit adds all.
    """
    patch = _git(
        repository,
        "diff-tree",
        "-p",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
        "--root",
        "--no-commit-id",
        sha,
    )
    return parse_added_lines(patch)


def parse_added_lines(patch: str) -> dict[str, list[str]]:
    """Read the added lines per file out of a unified diff."""
    result: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_hunk = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            current, in_hunk = None, False
        elif line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:]
            if target == "/dev/null":
                current = None
            else:
                path = target[2:] if target.startswith("b/") else target
                current = result.setdefault(path, [])
        elif in_hunk and current is not None and line.startswith("+"):
            current.append(line[1:].rstrip("\r"))
    return result
