"""What Git says about the folder a chat runs in: the remote, the developer's Git identity, HEAD.

These facts name the chat's project (the ``origin`` remote; see :mod:`ccp.db.hierarchy` on the
server) and the commit a session started and ended on. They are read with
:class:`ccp.capture.snapshots.git.Git` (no shell, no password prompts) and a short time limit,
because the hooks that ask for them hold up Claude Code.

Nothing here ever fails: a folder that is not a repository, a repository without ``origin``, a
missing ``git`` program or a slow disk all simply give "unknown" (``None``).

A remote URL can carry a user name and password (``https://user:token@host/repo.git``). Those are
removed before the URL leaves this module (:func:`strip_credentials`), and Git's own error output,
which may repeat the URL, is never read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ccp.capture.snapshots.git import Git, GitError

# Time limit for each Git call made from a hook (seconds).
GIT_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class GitFacts:
    """Facts about a working folder; each one is ``None`` if unknown.

    Attributes:
        remote: The ``origin`` remote URL without credentials.
        user_name: ``git config user.name``.
        user_email: ``git config user.email``.
    """

    remote: str | None = None
    user_name: str | None = None
    user_email: str | None = None


def strip_credentials(url: str) -> str:
    """Remove the user name and password from a URL with a scheme.

    ``https://user:token@host/acme/app.git`` becomes ``https://host/acme/app.git``. The short form
    ``git@host:acme/app.git`` has no password and is returned as it is (``git`` is not a secret).
    """
    scheme, separator, rest = url.strip().partition("://")
    if not separator:
        return url.strip()
    authority, slash, path = rest.partition("/")
    host = authority.rpartition("@")[2]
    return f"{scheme}://{host}{slash}{path}"


def read_git_facts(folder: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> GitFacts:
    """Read the remote and the Git identity for ``folder``.

    The identity is read even outside a repository (it usually comes from the global Git settings).

    Args:
        folder: The chat's working folder.
        timeout: Time limit for each Git call.
    """
    git = Git(folder, timeout)
    try:
        remote: str | None = strip_credentials(git.run("remote", "get-url", "origin")) or None
    except GitError:  # not a repository, no origin, or git missing
        remote = None
    user_name, user_email = git.identity()
    return GitFacts(remote=remote, user_name=user_name, user_email=user_email)


def read_head(folder: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> str | None:
    """Return the commit HEAD points to in ``folder``, or ``None`` (no repository or commit)."""
    return Git(folder, timeout).head()
