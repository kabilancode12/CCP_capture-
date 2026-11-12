"""A small, safe wrapper around the ``git`` command.

Everything the snapshot code asks of Git goes through :class:`Git`. It never uses a shell, never
lets Git prompt for a password (a hook has nobody to answer), and puts a time limit on every call.

Error messages name the Git command and nothing else. Git's own error text can contain a remote URL
with credentials in it (``https://user:token@host/...``), so it is never copied into a message or
a log.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 15.0


class GitError(RuntimeError):
    """Raised when a Git command fails or times out. The message never contains Git's output."""


def _environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    """Environment for a Git call: no prompts, no optional index locking, plus ``extra``."""
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # never wait for a password
        "GIT_OPTIONAL_LOCKS": "0",  # never fight the developer's own git commands for a lock
        **(extra or {}),
    }


class _Result:
    """The outcome of one Git call, with output decoded as UTF-8."""

    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout.decode("utf-8", errors="replace")


def _run(
    arguments: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> _Result:
    """Run ``git`` with ``arguments``; raise :class:`GitError` if it cannot be run or times out.

        Input and output are handled as **bytes**, not text. Python's text mode would turn every
        ``
    `` we send into ``
    `` on Windows, which Git reads as part of the value (a commit
        message full of carriage returns, an object id that Git does not recognise).
    """
    try:
        completed = subprocess.run(  # noqa: S603  (fixed program, list of arguments, no shell)
            ["git", "-c", "core.quotepath=false", *arguments],  # noqa: S607  (git is found on PATH)
            capture_output=True,
            input=None if input_text is None else input_text.encode("utf-8"),
            timeout=timeout,
            env=_environment(env),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {arguments[0]} timed out") from error
    except OSError as error:  # git is not installed, or cannot be started
        raise GitError(f"git cannot be run ({type(error).__name__})") from error
    return _Result(completed.returncode, completed.stdout)


def find_repository_root(directory: Path) -> Path | None:
    """Return the top-level folder of the Git repository containing ``directory``, or ``None``.

    ``None`` also covers "git is not installed": there is no repository to snapshot then.
    """
    try:
        result = _run(
            ["-C", str(directory), "rev-parse", "--show-toplevel"], timeout=DEFAULT_TIMEOUT_SECONDS
        )
    except GitError:
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


class Git:
    """Runs Git commands inside one repository."""

    def __init__(self, root: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        """Use the repository at ``root``; each call may take at most ``timeout`` seconds."""
        self.root = root
        self._timeout = timeout

    def run(
        self,
        *arguments: str,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run a Git command and return its standard output (without the final newline).

        Raises:
            GitError: If the command fails (non-zero exit) or times out. The message has the
                command name only.
        """
        result = _run(
            ["-C", str(self.root), *arguments],
            timeout=self._timeout if timeout is None else timeout,
            env=env,
            input_text=input_text,
        )
        if result.returncode != 0:
            raise GitError(f"git {arguments[0]} failed (exit {result.returncode})")
        return result.stdout.rstrip("\n")

    def succeeds(self, *arguments: str) -> bool:
        """Run a Git command and return whether it exited with 0 (any output is ignored)."""
        try:
            self.run(*arguments)
        except GitError:
            return False
        return True

    def head(self) -> str | None:
        """Return the commit HEAD points to, or ``None`` in a repository with no commits yet."""
        try:
            return self.run("rev-parse", "-q", "--verify", "HEAD")
        except GitError:
            return None

    def branch(self) -> str:
        """Return the current branch name, or ``detached`` if HEAD is not on a branch."""
        try:
            return self.run("symbolic-ref", "-q", "--short", "HEAD")
        except GitError:
            return "detached"

    def identity(self) -> tuple[str | None, str | None]:
        """Return the developer's ``(user.name, user.email)`` with a single Git call.

        A value that is not configured is ``None``.
        """
        try:
            output = self.run("config", "--get-regexp", r"^user\.(name|email)$")
        except GitError:  # exit 1 when neither is set
            return None, None
        values = dict(line.partition(" ")[::2] for line in output.splitlines())
        return values.get("user.name") or None, values.get("user.email") or None

    def config(self, key: str) -> str | None:
        """Return a Git configuration value, or ``None`` if it is not set."""
        try:
            return self.run("config", key) or None
        except GitError:
            return None
