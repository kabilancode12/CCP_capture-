"""Reading the raw lines of a chat back from the object store.

A chat's raw objects are ``sessions/<id>/<stream>/<offset>.jsonl`` chunks: stream ``main`` is the
chat, ``agent-<agent id>`` a sub-agent, ``agent-<agent id>.meta`` the sub-agent's description (not
a transcript; the database has it). The offsets are zero-padded, so the alphabetical order of the
keys is the order of the lines. A large attachment value that capture moved out of a line (a
``$ccp_ref`` pointer) is put back, so the catalog shows the line as Claude Code wrote it; if the
value is missing the pointer stays.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, Protocol

from ccp.index.agents import is_meta_stream
from ccp.storage import StorageError
from ccp.storage.attachments import POINTER_KEY, restore_line
from ccp.storage.keys import session_prefix, stream_of


class RawStore(Protocol):
    """The part of the object store this module reads."""

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under ``prefix``, in alphabetical order."""

    def get(self, key: str) -> bytes:
        """The content of an object."""

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Needed by the attachment helper's interface; never called here."""


def transcript_streams(store: RawStore, session_id: str) -> dict[str, list[str]]:
    """The chunk keys of each transcript stream of a chat (``main``, ``agent-<id>``), in order.

    Description streams (``agent-<id>.meta``) and tool-result files are left out.
    """
    streams: dict[str, list[str]] = {}
    for key in store.list_keys(session_prefix(session_id)):
        if not key.endswith(".jsonl"):
            continue
        stream = stream_of(key)
        if not is_meta_stream(stream):
            streams.setdefault(stream, []).append(key)
    return streams


def read_lines(store: RawStore, keys: list[str]) -> list[Any]:
    """The lines of the given chunks as parsed JSON, one item per original line, in order.

    A line that cannot be read (not UTF-8, not JSON) becomes
    ``{"type": "ccp-unreadable-line", "object": <key>, "line": <number>}``, so the array has one
    item per line and nothing is lost silently.
    """
    items: list[Any] = []
    for key in keys:
        # The last piece after the final newline is empty, as in the indexer.
        for number, raw in enumerate(store.get(key).split(b"\n")[:-1], start=1):
            items.append(_parse(store, raw, key, number))
    return items


def _parse(store: RawStore, raw: bytes, key: str, number: int) -> Any:
    try:
        text = raw.decode("utf-8")
        if POINTER_KEY in text:
            with suppress(StorageError):  # a missing value: keep the pointer
                text = restore_line(text, store)
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"type": "ccp-unreadable-line", "object": key, "line": number}
