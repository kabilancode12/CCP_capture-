"""Read the new, complete lines of a transcript file.

Claude Code appends to its transcript while the client reads it, so the last line may be half
written. Only **complete lines** (ending in a newline) are returned; a partial last line waits
for the next sync. Positions are byte offsets in the source file.

Two unusual files never block a stream:

* a single line longer than the read window is skipped without being held in memory, and a
  ``ccp-withheld`` placeholder (giving only the line's size, never its content) stands in for it;
* a file shorter than the saved position (truncated or rewritten) is logged and reads as
  "nothing new".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger("ccp.capture")

# Most bytes read in one call, to keep memory bounded for very large transcripts.
DEFAULT_WINDOW_BYTES = 16 * 1024 * 1024

# Bytes read at a time while looking for the end of a line longer than the window.
_SCAN_BLOCK_BYTES = 1024 * 1024


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


def oversized_line_placeholder(size: int) -> str:
    """Return the line uploaded in place of a line too long to read.

    Args:
        size: The skipped line's length in bytes, without its newline. Only this number is kept.
    """
    return json.dumps({"type": "ccp-withheld", "reason": "line too long to read", "bytes": size})


def read_complete_lines(
    path: Path, offset: int, window_bytes: int = DEFAULT_WINDOW_BYTES
) -> ReadResult:
    """Read complete lines starting at byte ``offset``.

    Args:
        path: The transcript file.
        offset: Where to start (the end of the last uploaded line).
        window_bytes: The most bytes to read in this call.

    Returns:
        The complete lines. If the first line is longer than the window, a single placeholder
        line (see :func:`oversized_line_placeholder`) that covers its source bytes. Nothing if
        the file is shorter than ``offset``.

    Raises:
        OSError: If the file cannot be read (``FileNotFoundError`` if it does not exist).
    """
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        if offset > size:
            logger.warning(
                "transcript %s is shorter (%d bytes) than the uploaded position (%d): "
                "it was truncated or rewritten; nothing new is read",
                path.name,
                size,
                offset,
            )
            return ReadResult([], reached_end=True)
        handle.seek(offset)
        data = handle.read(window_bytes)
        last_newline = data.rfind(b"\n")
        if last_newline == -1 and len(data) >= window_bytes:
            return _skip_oversized_line(handle, path, len(data))

    if last_newline == -1:
        return ReadResult([], reached_end=True)  # only a partial line so far

    complete = data[: last_newline + 1]
    lines = [
        SourceLine(raw.decode("utf-8", errors="replace"), len(raw) + 1)
        for raw in complete.split(b"\n")[:-1]
    ]
    window_full = len(data) >= window_bytes
    return ReadResult(lines, reached_end=not window_full)


def _skip_oversized_line(handle: BinaryIO, path: Path, already_read: int) -> ReadResult:
    """Find the end of a line that filled the whole read window, reading block by block.

    Only the line's length is kept; its content is never held in memory as a whole.

    Args:
        handle: The open file, positioned just after the ``already_read`` bytes of the line.
        path: The file (its name is logged).
        already_read: How many bytes of the line were read so far (none was a newline).

    Returns:
        One placeholder line covering the whole source line and its newline, or nothing if the
        line is not finished yet (Claude Code is still writing it).
    """
    length = already_read
    while True:
        block = handle.read(_SCAN_BLOCK_BYTES)
        if not block:
            return ReadResult([], reached_end=True)  # not finished yet: wait for the newline
        newline = block.find(b"\n")
        if newline != -1:
            length += newline
            break
        length += len(block)
    logger.warning(
        "transcript %s has a line of %d bytes, longer than the read window; "
        "a placeholder is uploaded in its place",
        path.name,
        length,
    )
    placeholder = SourceLine(oversized_line_placeholder(length), length + 1)
    return ReadResult([placeholder], reached_end=False)  # more lines may follow it


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
