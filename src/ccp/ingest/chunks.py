"""Checks on an uploaded transcript chunk.

The client is trusted to redact, but the server does not trust the shape of what it receives.
A chunk must be a whole number of complete lines, each a JSON object, so everything stored can be
parsed later without surprises. (The server does not look at the *content*; redaction happens on
the developer's machine.)
"""

from __future__ import annotations

import json


class ChunkError(ValueError):
    """Raised when an uploaded chunk is not well formed. The message is safe to show the client."""


def validate_chunk(body: bytes) -> None:
    """Check that ``body`` is UTF-8 text made of complete JSON-object lines.

    Args:
        body: The raw bytes of the uploaded chunk.

    Raises:
        ChunkError: If the chunk is empty, not UTF-8, does not end with a newline, or has a line
            that is not a JSON object. The error names the line number, never its content.
    """
    if not body:
        raise ChunkError("chunk is empty")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ChunkError("chunk is not valid UTF-8") from error
    if not text.endswith("\n"):
        raise ChunkError("chunk must end with a complete line (a final newline)")

    # Split on "\n" only: splitlines() would also split on characters (such as U+2028)
    # that JSON allows inside strings.
    for number, line in enumerate(text.split("\n")[:-1], start=1):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise ChunkError(f"line {number} is not valid JSON") from error
        if not isinstance(parsed, dict):
            raise ChunkError(f"line {number} is not a JSON object")
