"""Start a background process that keeps running after the caller exits.

Used for the best-effort flush at SessionEnd: Claude Code gives SessionEnd hooks very little time
before it exits, so the hook starts the upload in a separate process and returns at once.

This is *best effort*. On Windows the process is started outside Claude Code's job object when
that is allowed, but the operating system or a company policy may still end it with its parent.
Nothing may depend on it finishing: the catch-up at the next SessionStart uploads whatever is left.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

# Windows process-creation flags (numeric values, because the constants exist only on Windows).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def spawn_detached(argv: Sequence[str]) -> None:
    """Run ``argv`` in the background, detached from this process.

    Output is discarded and standard input is closed. The caller does not wait.

    Args:
        argv: The command and its arguments (a list: no shell is involved).

    Raises:
        OSError: If the process cannot be started at all.
    """
    command = list(argv)
    if os.name != "nt":
        subprocess.Popen(  # noqa: S603  (argv is a list built by our own code)
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        return

    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    try:
        # Outside the parent's job object, so closing Claude Code does not end the upload.
        _popen_windows(command, flags | _CREATE_BREAKAWAY_FROM_JOB)
    except OSError:  # breaking away is not allowed here: start it inside the job instead
        _popen_windows(command, flags)


def _popen_windows(command: list[str], creation_flags: int) -> None:
    """Start ``command`` on Windows with the given creation flags and no inherited streams."""
    subprocess.Popen(  # noqa: S603  (argv is a list built by our own code)
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
    )
