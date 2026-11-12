"""What the client remembers on the developer's machine.

For every stream (a session transcript, or one sub-agent) the client stores one number: how many
source bytes have been uploaded. The transcript file itself is the queue: the number only moves
forward after an upload fully succeeded, so after any failure the next sync simply starts from the
same place. Only this number is stored; no transcript content is ever copied to the state folder.

Layout::

    <state_dir>/<session_id>/<stream>.offset      the number (a plain integer)
    <state_dir>/<session_id>/<stream>.lock        exists while a sync of that stream runs
    <state_dir>/<session_id>/transcript_path      where the session transcript is (for catch-up)
    <state_dir>/<session_id>/<name>.marker        a small remembered value (for example a hash)
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ccp.storage.keys import check_id

# A lock older than this is assumed to belong to a crashed process and is taken over.
STALE_LOCK_SECONDS = 120.0


class StateStore:
    """Reads and writes the client's small state files."""

    def __init__(self, directory: Path) -> None:
        """Use ``directory`` as the state folder (created on first write)."""
        self._directory = directory

    def _session_dir(self, session_id: str) -> Path:
        # check_id rejects anything that could escape the folder (such as "../x").
        return self._directory / check_id("session id", session_id)

    def get_offset(self, session_id: str, stream: str) -> int:
        """Return the uploaded position of a stream (0 if nothing was uploaded yet)."""
        path = self._session_dir(session_id) / f"{check_id('stream', stream)}.offset"
        try:
            return max(0, int(path.read_text(encoding="utf-8").strip()))
        except (FileNotFoundError, ValueError):
            return 0

    def set_offset(self, session_id: str, stream: str, offset: int) -> None:
        """Save the uploaded position of a stream, atomically.

        The value is written to a temporary file and moved into place, so a crash leaves either the
        old or the new number, never a half-written one.
        """
        folder = self._session_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{check_id('stream', stream)}.offset"
        temporary = path.with_suffix(".offset.tmp")
        temporary.write_text(str(offset), encoding="utf-8")
        os.replace(temporary, path)

    def get_marker(self, session_id: str, name: str) -> str | None:
        """Return a small remembered value (such as a hash), or ``None`` if there is none."""
        path = self._session_dir(session_id) / f"{check_id('marker name', name)}.marker"
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def set_marker(self, session_id: str, name: str, value: str) -> None:
        """Remember a small value for a session, atomically."""
        folder = self._session_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{check_id('marker name', name)}.marker"
        temporary = path.with_suffix(".marker.tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)

    def remember_transcript(self, session_id: str, transcript_path: Path) -> None:
        """Remember where a session's transcript is, so a later catch-up can find it."""
        folder = self._session_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transcript_path").write_text(str(transcript_path), encoding="utf-8")

    def known_sessions(self) -> list[tuple[str, Path]]:
        """Return ``(session_id, transcript_path)`` for every session the client has seen."""
        if not self._directory.is_dir():
            return []
        found = []
        for folder in sorted(self._directory.iterdir()):
            pointer = folder / "transcript_path"
            if pointer.is_file():
                found.append((folder.name, Path(pointer.read_text(encoding="utf-8").strip())))
        return found

    @contextmanager
    def lock(self, session_id: str, stream: str, wait_seconds: float = 10.0) -> Iterator[bool]:
        """Hold the lock of one stream, so two hooks never upload the same stream at once.

        Two uploads of the same stream could send chunks of different length from the same
        offset (the file grows between the two reads), which the server rightly refuses.

        Args:
            session_id: The session.
            stream: The stream within the session.
            wait_seconds: How long to wait for another sync to finish.

        Yields:
            True if the lock was obtained, False if it could not be (another sync is running).
            The caller should skip the stream in that case: the other sync, or the next one,
            will carry on from the saved position.
        """
        folder = self._session_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{check_id('stream', stream)}.lock"
        acquired = _acquire(path, wait_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                path.unlink(missing_ok=True)


def _acquire(path: Path, wait_seconds: float) -> bool:
    """Create the lock file exclusively; wait up to ``wait_seconds``; take over a stale lock."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _is_stale(path):
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        else:
            os.write(descriptor, str(os.getpid()).encode())
            os.close(descriptor)
            return True


def _is_stale(path: Path) -> bool:
    """Return True if the lock file is older than :data:`STALE_LOCK_SECONDS`."""
    try:
        return time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS
    except FileNotFoundError:
        return False  # it just disappeared: the next attempt will succeed
