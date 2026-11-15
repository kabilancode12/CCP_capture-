"""Finding the text a test command printed.

A shell result holds the command's output in one of two ways:

* **inline**: the result's text is the output (Claude Code cuts very long output in the middle);
* **in a file**: for large output Claude Code writes it to ``<chat folder>/tool-results/<name>`` and
  the result holds only a ``<persisted-output>`` note that names that file and a short preview. The
  capture client uploads those files (``sessions/<id>/tool-results/<name>``, see
  :mod:`ccp.storage.keys`), possibly after the transcript, so the file may not be there yet.

:func:`read_test_output` returns the text and where it came from, which is stored as
``test_run.output_source`` (``inline``, ``tool_result_file`` or ``missing``). A preview is never
used: the summary line of a runner is at the end of the output, so a preview gives wrong counts.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from ccp.storage.keys import tool_result_key

INLINE = "inline"
TOOL_RESULT_FILE = "tool_result_file"
MISSING = "missing"

_NAME_IN_PATH = re.compile(r"tool-results[\\/]([A-Za-z0-9][A-Za-z0-9._-]*)")


class OutputStore(Protocol):
    """The part of the object store that holds tool-result files."""

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under ``prefix``, in alphabetical order."""

    def get(self, key: str) -> bytes:
        """The content of an object."""


def result_text(content: Any) -> str:
    """The text of a tool result: the ``content`` of its message (a string, or text blocks)."""
    if isinstance(content, str):
        return content
    pieces: list[str] = []
    for block in content if isinstance(content, list) else ():
        inner = block.get("content") if isinstance(block, dict) else None
        if isinstance(inner, str):
            pieces.append(inner)
        elif isinstance(inner, list):
            pieces.extend(
                part["text"]
                for part in inner
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return "\n".join(pieces)


def persisted_file_name(text: str, persisted_path: str | None) -> str | None:
    """The name of the tool-result file a result points to, or ``None`` if the output is inline.

    Args:
        text: The result's text.
        persisted_path: ``toolUseResult.persistedOutputPath``, when the transcript has it.
    """
    if persisted_path:
        match = _NAME_IN_PATH.search(persisted_path)
        if match:
            return match.group(1)
    if "<persisted-output>" in text:
        match = _NAME_IN_PATH.search(text)
        return match.group(1) if match else ""
    return None


def read_test_output(
    store: OutputStore, session_id: str, content: Any, persisted_path: str | None
) -> tuple[str | None, str]:
    """Get the output of a test command.

    Args:
        store: Where the uploaded tool-result files are.
        session_id: The chat.
        content: The result's ``message.content``.
        persisted_path: ``toolUseResult.persistedOutputPath``, if present.

    Returns:
        ``(text, source)``: the output and ``inline`` or ``tool_result_file``, or ``(None,
        'missing')`` when the output is in a file that has not been uploaded (or cannot be named).
    """
    text = result_text(content)
    name = persisted_file_name(text, persisted_path)
    if name is None:
        return text, INLINE
    try:
        key = tool_result_key(session_id, name)
    except ValueError:
        return None, MISSING
    if key not in store.list_keys(key):
        return None, MISSING
    return store.get(key).decode("utf-8", errors="replace"), TOOL_RESULT_FILE
