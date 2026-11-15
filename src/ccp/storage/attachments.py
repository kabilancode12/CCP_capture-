"""De-duplicate large attachment entries.

About 80 % of a Claude Code transcript is ``attachment`` entries (skill, tool and agent
listings, prompt snapshots), and the same content repeats in every session. This module moves
each large value out of an attachment line into the object store, under the SHA-256 hash of
its content, and leaves a small pointer in the line. A second copy of the same content is not
stored again.

Order in the pipeline: **redact first, then externalise**, so what is stored is already
clean, and the hash is of the clean content.

Exactness: :func:`restore_line` gives back the original line byte-for-byte. Claude Code writes
compact JSON, so re-serialising a parsed line reproduces it exactly. This is *checked* for every
line; a line that would not round-trip is left untouched instead of risking a change.

Observed on Claude Code 2.1.289 ([verify] on other versions): in ``attachment`` entries the large
values are inside the ``attachment`` object (such as a skill listing's ``content`` or a prompt
snapshot's ``systemPrompt``) and ``rendered``.

Two levels are looked at:

* the entry's own values (such as ``rendered``): a large one is replaced by a pointer;
* inside the ``attachment`` object: each large value is replaced by a pointer, while the small
  ones (above all its ``type``) stay in the line, so they can still be queried::

    {"type":"attachment",...,"attachment":{"type":"skill_listing","content":{"$ccp_ref":"<sha256>"}}}

Older lines replaced the whole ``attachment`` object by one pointer
(``"attachment":{"$ccp_ref":"<sha256>"}``); :func:`restore_line` still rebuilds those.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ccp.storage.keys import attachment_key

# Values smaller than this stay in the line: a pointer would not save anything.
MIN_EXTERNAL_BYTES = 1024

# A pointer replaces the value: {"$ccp_ref": "<sha256>"}.
POINTER_KEY = "$ccp_ref"

# The entry key whose object value is opened up: its large values are moved out one by one.
_NESTED_KEY = "attachment"

# Fields of an entry (or of its attachment object) that identify it; never moved out, whatever
# their size.
_PROTECTED_KEYS = frozenset({"type", "uuid", "parentUuid", "sessionId", "session_id", "timestamp"})
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


class BlobStore(Protocol):
    """The part of the object store this module needs."""

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Store ``data`` unless ``key`` exists; return True if it was written."""

    def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""


@dataclass(frozen=True)
class ExternalisedLine:
    """The outcome of :func:`externalise_line`.

    Attributes:
        text: The line to store in the transcript (the original if nothing was moved).
        new_objects: How many values were stored for the first time.
        reused_objects: How many values were already stored (the saving).
    """

    text: str
    new_objects: int = 0
    reused_objects: int = 0


def _serialise(value: Any) -> str:
    """Serialise exactly as Claude Code does: compact, with real (non-escaped) characters."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_pointer(value: Any) -> bool:
    """Return True if ``value`` is a pointer written by this module."""
    return (
        isinstance(value, dict)
        and list(value) == [POINTER_KEY]
        and isinstance(value[POINTER_KEY], str)
        and _SHA256_HEX.fullmatch(value[POINTER_KEY]) is not None
    )


def externalise_line(raw_line: str, store: BlobStore) -> ExternalisedLine:
    """Move the large values of an attachment line into the store.

    Args:
        raw_line: One transcript line, **already redacted**.
        store: Where the values are stored.

    Returns:
        The line with pointers in place of large values (see the module description for which
        values). Lines that are not JSON objects, are not attachments, hold nothing large, or
        would not round-trip exactly are returned unchanged.
    """
    unchanged = ExternalisedLine(raw_line)
    try:
        entry = json.loads(raw_line)
    except json.JSONDecodeError:
        return unchanged
    if not isinstance(entry, dict) or entry.get("type") != "attachment":
        return unchanged
    if _serialise(entry) != raw_line:  # not in Claude Code's own format: leave it alone
        return unchanged

    bodies: dict[str, bytes] = {}  # digest -> bytes, for the values moved out of this line
    moved: dict[str, Any] = {}
    for key, value in entry.items():
        if _is_nested(key, value):
            moved[key] = {k: _move_if_large(k, v, bodies) for k, v in value.items()}
        else:
            moved[key] = _move_if_large(key, value, bodies)
    if not bodies:
        return unchanged

    # Prove the line can be rebuilt exactly before anything is stored.
    rebuilt = _restore_entry(moved, lambda digest: bodies[digest])
    if _serialise(rebuilt) != raw_line:
        return unchanged

    new = 0
    for digest, body in bodies.items():
        new += store.put_if_absent(attachment_key(digest), body)
    return ExternalisedLine(_serialise(moved), new_objects=new, reused_objects=len(bodies) - new)


def _is_nested(key: str, value: Any) -> bool:
    """Return True if ``value`` is an attachment object whose own values are looked at."""
    return key == _NESTED_KEY and isinstance(value, dict) and not _is_pointer(value)


def _move_if_large(key: str, value: Any, bodies: dict[str, bytes]) -> Any:
    """Return a pointer in place of a large ``value`` (its body is added to ``bodies``).

    Identifying fields, existing pointers and small values are returned unchanged.
    """
    if key in _PROTECTED_KEYS or _is_pointer(value):
        return value
    body = _serialise(value).encode("utf-8")
    if len(body) < MIN_EXTERNAL_BYTES:
        return value
    digest = hashlib.sha256(body).hexdigest()
    bodies[digest] = body
    return {POINTER_KEY: digest}


def _has_pointer(entry: dict[str, Any]) -> bool:
    """Return True if ``entry`` holds a pointer, in the entry itself or in its attachment."""
    for key, value in entry.items():
        if _is_pointer(value):
            return True
        if _is_nested(key, value) and any(_is_pointer(v) for v in value.values()):
            return True
    return False


def _restore_entry(entry: dict[str, Any], load: Callable[[str], bytes]) -> dict[str, Any]:
    """Replace every pointer in ``entry`` by the value ``load(digest)`` returns (as bytes).

    Pointers are looked for where :func:`externalise_line` puts them: among the entry's values
    (also the old form, where the whole attachment object is one pointer) and among the values
    of its attachment object.
    """
    restored: dict[str, Any] = {}
    for key, value in entry.items():
        if _is_nested(key, value):
            restored[key] = {k: _restore_value(v, load) for k, v in value.items()}
        else:
            restored[key] = _restore_value(value, load)
    return restored


def _restore_value(value: Any, load: Callable[[str], bytes]) -> Any:
    """Return the stored value if ``value`` is a pointer, otherwise ``value`` itself."""
    if _is_pointer(value):
        return json.loads(load(value[POINTER_KEY]).decode("utf-8"))
    return value


def restore_line(line: str, store: BlobStore) -> str:
    """Rebuild the original line from a line made by :func:`externalise_line`.

    Both forms are rebuilt: the current one (pointers inside the attachment object) and the
    older one (the whole attachment object as one pointer). Lines without pointers are returned
    unchanged.

    Raises:
        ccp.storage.object_store.ObjectNotFoundError: If a referenced value is missing from
            the store (the store raises it from ``get``).
    """
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(entry, dict) or not _has_pointer(entry):
        return line
    return _serialise(_restore_entry(entry, lambda digest: store.get(attachment_key(digest))))
