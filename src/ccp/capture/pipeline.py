"""Turn source lines into one uploadable chunk: redact, de-duplicate attachments, check.

Order matters: **redact first, then move large attachment values out**, so stored attachment bodies
are already clean. Nothing here touches the network or the disk; :mod:`ccp.capture.client` does
the uploading.

Two safeguards keep a bad line from blocking a stream forever, while never uploading it as is:

* a line the redactor could not process is replaced by a ``ccp-withheld`` placeholder;
* a line that is not a JSON object (a corrupt line) is replaced by a ``ccp-invalid-line``
  placeholder, because the server accepts only JSON-object lines.

Each replacement is reported as a redaction record, so it is visible in the log.

A chunk the API keeps refusing is replaced as a whole by :func:`refused_chunk` (see
:mod:`ccp.capture.client`), and a line too long to read by the reader's own placeholder (see
:mod:`ccp.capture.reader`); both use the same ``ccp-withheld`` form.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from ccp.capture.reader import SourceLine
from ccp.redaction import Hit, redact_line
from ccp.redaction.records import RedactionRecord, records_from_hits
from ccp.storage.attachments import externalise_line

WITHHELD_RULE = "withheld-line"
INVALID_RULE = "invalid-line"
_WITHHELD_LINE = json.dumps({"type": "ccp-withheld", "reason": "redaction failed"})
_INVALID_LINE = json.dumps({"type": "ccp-invalid-line", "reason": "not a JSON object"})


@dataclass(frozen=True)
class PreparedChunk:
    """A chunk ready to upload.

    Attributes:
        body: The text to upload (complete JSON lines); empty if the source lines were all blank.
        attachments: Attachment bodies moved out of the lines, by SHA-256 digest. They must be
            uploaded **before** the chunk, which refers to them.
        records: Where redaction rules fired, ready to report.
        source_bytes: How many bytes of the source file this chunk covers.
    """

    body: bytes
    attachments: dict[str, bytes]
    records: list[RedactionRecord]
    source_bytes: int


class _Collector:
    """Stands in for the object store: remembers the bodies instead of storing them."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Remember ``data`` under its digest (the last part of the key)."""
        self.bodies[key.rsplit("/", 1)[1]] = data
        return True

    def get(self, key: str) -> bytes:
        """Not used when uploading."""
        raise KeyError(key)


def prepare_chunk(chunk_key: str, lines: Sequence[SourceLine]) -> PreparedChunk:
    """Clean the lines of one chunk.

    Args:
        chunk_key: The object key the chunk will be stored under (used in redaction locations).
        lines: The source lines. Blank lines are skipped.

    Returns:
        The prepared chunk.
    """
    collector = _Collector()
    output: list[str] = []
    hits: list[Hit] = []

    for source in lines:
        if not source.text.strip():
            continue  # a blank line carries nothing; its bytes still count as consumed
        number = len(output) + 1  # position of the line within the uploaded chunk
        result = redact_line(source.text, number)
        if result.withheld or result.text is None:
            hits.append(Hit(WITHHELD_RULE, number, ""))
            output.append(_WITHHELD_LINE)
            continue
        hits.extend(result.hits)
        text = externalise_line(result.text, collector).text
        if not _is_json_object(text):
            hits.append(Hit(INVALID_RULE, number, ""))
            text = _INVALID_LINE
        output.append(text)

    body = ("\n".join(output) + "\n").encode("utf-8") if output else b""
    return PreparedChunk(
        body=body,
        attachments=collector.bodies,
        records=records_from_hits(hits, chunk_key),
        source_bytes=sum(line.size for line in lines),
    )


def refused_chunk(lines: Sequence[SourceLine], status: int) -> PreparedChunk:
    """Return a one-line placeholder chunk that stands in for a chunk the API keeps refusing.

    It covers the same source bytes, so the stream can move past the chunk. Only counts and the
    HTTP status are kept, never content.

    Args:
        lines: The source lines of the refused chunk.
        status: The HTTP status of the last refusal (for example 413 or 422).
    """
    source_bytes = sum(line.size for line in lines)
    placeholder = {
        "type": "ccp-withheld",
        "reason": "chunk refused by the API",
        "status": status,
        "lines": len(lines),
        "bytes": source_bytes,
    }
    return PreparedChunk(
        body=(json.dumps(placeholder) + "\n").encode("utf-8"),
        attachments={},
        records=[],
        source_bytes=source_bytes,
    )


def _is_json_object(text: str) -> bool:
    """Return True if ``text`` is one JSON object."""
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False
