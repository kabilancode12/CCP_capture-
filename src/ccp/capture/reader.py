"""Read the new, complete lines of a transcript file.

Claude Code appends to its transcript while the client reads it, so the last line may be half
written. Only **complete lines** (ending in a newline) are returned; a partial last line waits
for the next sync. Positions are byte offsets in the source file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Most bytes read in one call, to keep memory bounded for very large transcripts.
DEFAULT_WINDOW_BYTES = 16 * 1024 * 1024


class TranscriptError(RuntimeError):
    """Raised when a transcript cannot be read as expected."""


@dataclass(frozen=True)
class SourceLine:
    """One line of the source file.

    Attributes:
        text: The line without its newline (invalid UTF-8 bytes are replaced).
        size: How many source bytes the line occupies, including the newline.
    """

    text: str
    size: int


@dataclass(frozen=True)
class ReadResult:
    """New lines read from a file.

    Attributes:
        lines: The complete lines found after the starting offset.
        reached_end: True if nothing complete is left in the file; False if the read window was
            full and more complete lines may follow.
    """

    lines: list[SourceLine]
    reached_end: bool


def read_complete_lines(
    path: Path, offset: int, window_bytes: int = DEFAULT_WINDOW_BYTES
) -> ReadResult:
    """Read complete lines starting at byte ``offset``.

    Args:
        path: The transcript file.
        offset: Where to start (the end of the last uploaded line).
        window_bytes: The most bytes to read in this call.

    Raises:
        TranscriptError: If the file is shorter than ``offset`` (it was replaced or truncated), or
            a single line is longer than the read window.
        FileNotFoundError: If the file does not exist.
    """
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        if offset > size:
            raise TranscriptError("transcript is shorter than the uploaded position")
        handle.seek(offset)
        data = handle.read(window_bytes)

    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        if len(data) >= window_bytes:
            raise TranscriptError("a single line is longer than the read window")
        return ReadResult([], reached_end=True)  # only a partial line so far

    complete = data[: last_newline + 1]
    lines = [
        SourceLine(raw.decode("utf-8", errors="replace"), len(raw) + 1)
        for raw in complete.split(b"\n")[:-1]
    ]
    window_full = len(data) >= window_bytes
    return ReadResult(lines, reached_end=not window_full)


def split_into_chunks(lines: Sequence[SourceLine], max_bytes: int) -> list[list[SourceLine]]:
    """Group lines into chunks of at most ``max_bytes`` source bytes (never splitting a line).

    A single line larger than ``max_bytes`` becomes a chunk of its own.
    """
    chunks: list[list[SourceLine]] = []
    current: list[SourceLine] = []
    current_size = 0
    for line in lines:
        if current and current_size + line.size > max_bytes:
            chunks.append(current)
            current, current_size = [], 0
        current.append(line)
        current_size += line.size
    if current:
        chunks.append(current)
    return chunks
