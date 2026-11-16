"""Global Git hooks: a note on every commit, and a backup before every push.

``ccp-capture git-hooks install`` writes two small scripts to ``~/.ccp/git-hooks`` and points Git's
``core.hooksPath`` at them **only if the developer has not set one**. Each script runs the
capture client (silently, never failing the commit or the push) and then hands over to the
repository's own hook of the same name, so a project's hooks keep working. The commit itself is
never changed: the link to the capture chain is a Git note in ``refs/notes/ccp``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HOOK_NAMES = ("post-commit", "pre-push")
FOLDER = Path.home() / ".ccp" / "git-hooks"

_SCRIPT = """#!/bin/sh
# Installed by ccp-capture. Records the commit or backs up before the push; never blocks Git.
"{python}" -m ccp.capture git-hook {name} </dev/null >/dev/null 2>&1 || true
own="$(git rev-parse --git-dir 2>/dev/null)/hooks/{name}"
if [ -x "$own" ]; then exec "$own" "$@"; fi
exit 0
"""


def _git_config(*arguments: str) -> str:
    done = subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [shutil.which("git") or "git", "config", "--global", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def install(folder: Path = FOLDER) -> list[str]:
    """Write the hook scripts and set ``core.hooksPath`` if it is free; return notes."""
    folder.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable).as_posix()
    for name in HOOK_NAMES:
        script = folder / name
        script.write_text(_SCRIPT.format(python=python, name=name), encoding="utf-8", newline="\n")
        script.chmod(0o755)
    notes = [f"hook scripts written to {folder}"]
    current = _git_config("--get", "core.hooksPath")
    if not current:
        _git_config("core.hooksPath", folder.as_posix())
        notes.append("core.hooksPath set (global)")
    elif Path(current).expanduser() == folder:
        notes.append("core.hooksPath already points here")
    else:
        notes.append(
            f"core.hooksPath is already {current}: left unchanged. Call the scripts in {folder} "
            "from your own hooks to turn this on."
        )
    return notes


def uninstall(folder: Path = FOLDER) -> list[str]:
    """Remove the scripts, and ``core.hooksPath`` if it points at them."""
    notes = []
    if Path(_git_config("--get", "core.hooksPath")).expanduser() == folder:
        _git_config("--unset", "core.hooksPath")
        notes.append("core.hooksPath removed")
    for name in HOOK_NAMES:
        (folder / name).unlink(missing_ok=True)
    return [*notes, "hook scripts removed"]
