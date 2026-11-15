"""Where things are stored: the object-key layout.

Keys are built only here, so the layout is documented and checked in one place. Every part that
comes from outside (session ids, agent ids) is validated, so a hostile value such as
``../other`` can never escape its folder.

Layout::

    sessions/<session_id>/<stream>/<offset>.jsonl     a chunk of a transcript
    sessions/<session_id>/tool-results/<name>         a large tool output Claude Code kept in a file
    sessions/<session_id>/tool-results/<name>@<hash>  a later, changed version of that file
    attachments/<first 2 hex>/<sha256>                an attachment body, stored once

``tool-results`` is therefore not allowed as a stream name, and a tool-result file may not end in
``.jsonl``: either would make a tool output look like a transcript chunk to the indexer.
"""

from __future__ import annotations

import re

# Letters, digits, dot, underscore, hyphen; must start with a letter or digit.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

MAIN_STREAM = "main"
TOOL_RESULTS_FOLDER = "tool-results"


def check_id(label: str, value: str) -> str:
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
        ValueError: If an id is unsafe, the stream is ``tool-results``, or the offset is negative.
    """
    if offset < 0:
        raise ValueError("Invalid offset: must be 0 or more")
    safe_session = check_id("session id", session_id)
    safe_stream = check_id("stream", stream)
    if safe_stream == TOOL_RESULTS_FOLDER:
        raise ValueError(f"Invalid stream: {TOOL_RESULTS_FOLDER} is reserved")
    # Zero-padded so that listing the keys in alphabetical order gives the chunks in order.
    return f"sessions/{safe_session}/{safe_stream}/{offset:012d}.jsonl"


def session_prefix(session_id: str) -> str:
    """Return the key prefix under which everything of one session is stored."""
    return f"sessions/{check_id('session id', session_id)}/"


def tool_result_key(session_id: str, name: str) -> str:
    """Return the key of a tool-result file of a session.

    Claude Code keeps large tool outputs in ``<chat folder>/tool-results/<name>`` and only refers
    to them from the transcript. Each file is stored under its own name, so uploading it again
    writes to the same key. Stored objects are never replaced: a changed file is stored next to it
    (:func:`tool_result_version_key`).

    Args:
        session_id: Claude Code session id.
        name: The file name: letters, digits, ``.``, ``_`` or ``-``, at most 128 characters, not
            ending in ``.jsonl`` (observed names look like ``bs12jynbi.txt``).

    Raises:
        ValueError: If the id or the name is not acceptable.
    """
    safe_name = check_id("tool-result name", name)
    if safe_name.endswith(".jsonl"):
        raise ValueError("Invalid tool-result name: must not end in .jsonl")
    return f"{session_prefix(session_id)}{TOOL_RESULTS_FOLDER}/{safe_name}"


def attachment_key(digest: str) -> str:
    """Return the key of an attachment body, from its SHA-256 digest (64 lowercase hex digits).

    Raises:
        ValueError: If ``digest`` is not a SHA-256 hex digest.
    """
    if not _SHA256_HEX.fullmatch(digest):
        raise ValueError("Invalid digest: must be 64 lowercase hex digits")
    return f"attachments/{digest[:2]}/{digest}"


def stream_of(key: str) -> str:
    """Return the stream part of a transcript chunk key, such as ``main`` or ``agent-a1``.

    Args:
        key: A key made by :func:`transcript_chunk_key`.

    Raises:
        ValueError: If ``key`` is not a transcript chunk key.
    """
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "sessions":
        raise ValueError("Not a transcript chunk key")
    return parts[2]


def tool_result_version_key(key: str, digest: str) -> str:
    """Return the key of a changed version of a tool-result file.

    The first version of a file is stored at :func:`tool_result_key`; a later version with other
    content is stored as ``<that key>@<first 16 hex digits of its SHA-256>``, so nothing stored is
    ever replaced. The name never ends in ``.jsonl``, so the indexer never takes it for a chunk.

    Args:
        key: The file's key (from :func:`tool_result_key`).
        digest: The SHA-256 of the new version's content (64 lowercase hex digits).

    Raises:
        ValueError: If ``digest`` is not a SHA-256 hex digest.
    """
    if not _SHA256_HEX.fullmatch(digest):
        raise ValueError("Invalid digest: must be 64 lowercase hex digits")
    return f"{key}@{digest[:16]}"
