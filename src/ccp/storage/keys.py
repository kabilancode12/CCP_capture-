"""Where things are stored: the object-key layout.

Keys are built only here, so the layout is documented and checked in one place. Every part that
comes from outside (session ids, agent ids) is validated, so a hostile value such as
``../other`` can never escape its folder.

Layout::

    sessions/<session_id>/<stream>/<offset>.jsonl     a chunk of a transcript
    attachments/<first 2 hex>/<sha256>                an attachment body, stored once
"""

from __future__ import annotations

import re

# Letters, digits, dot, underscore, hyphen; must start with a letter or digit.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

MAIN_STREAM = "main"


def _check_id(label: str, value: str) -> str:
    """Return ``value`` if it is a safe identifier, else raise ``ValueError``."""
    if not _SAFE_ID.fullmatch(value) or ".." in value:
        raise ValueError(f"Invalid {label}: must be letters, digits, '.', '_' or '-'")
    return value


def transcript_chunk_key(session_id: str, stream: str, offset: int) -> str:
    """Return the key of one transcript chunk.

    A chunk is identified by the session, the stream and the byte offset in the source file
    where it starts. Uploading the same chunk again therefore writes to the same key: uploads
    are idempotent.

    Args:
        session_id: Claude Code session id.
        stream: ``"main"`` for the session transcript, or ``agent-<agent_id>`` for a sub-agent.
        offset: Byte offset (0 or more) of the chunk's first byte in the source file.

    Raises:
        ValueError: If an id is unsafe or the offset is negative.
    """
    if offset < 0:
        raise ValueError("Invalid offset: must be 0 or more")
    safe_session = _check_id("session id", session_id)
    safe_stream = _check_id("stream", stream)
    # Zero-padded so that listing the keys in alphabetical order gives the chunks in order.
    return f"sessions/{safe_session}/{safe_stream}/{offset:012d}.jsonl"


def session_prefix(session_id: str) -> str:
    """Return the key prefix under which everything of one session is stored."""
    return f"sessions/{_check_id('session id', session_id)}/"


def attachment_key(digest: str) -> str:
    """Return the key of an attachment body, from its SHA-256 digest (64 lowercase hex digits).

    Raises:
        ValueError: If ``digest`` is not a SHA-256 hex digest.
    """
    if not _SHA256_HEX.fullmatch(digest):
        raise ValueError("Invalid digest: must be 64 lowercase hex digits")
    return f"attachments/{digest[:2]}/{digest}"
