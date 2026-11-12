"""Redaction records: what the client reports to the server, and what ends up in the log.

A record says that a rule fired somewhere, and how often. It never contains the redacted value.
Its location is ``<chunk object key>:<line in the chunk>:<JSON path>`` so a reviewer can find the
place in the stored transcript.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from ccp.redaction.redactor import Hit


@dataclass(frozen=True)
class RedactionRecord:
    """One redaction site.

    Attributes:
        rule_id: The rule that fired.
        location: Where, as described in the module docstring.
        occurrences: How many times the rule fired at that location (2 for two emails in one
            text).
    """

    rule_id: str
    location: str
    occurrences: int = 1


def records_from_hits(hits: Iterable[Hit], chunk_key: str) -> list[RedactionRecord]:
    """Turn the hits of one uploaded chunk into records, counting repeats.

    Args:
        hits: Hits from :func:`ccp.redaction.redact_line`; ``Hit.line`` is the 1-based line within
            the chunk.
        chunk_key: The object key the chunk is stored under.

    Returns:
        One record per distinct (rule, location), in a stable order.
    """
    counts = Counter((hit.rule_id, f"{chunk_key}:{hit.line}:{hit.path}") for hit in hits)
    return [RedactionRecord(rule, location, n) for (rule, location), n in sorted(counts.items())]
