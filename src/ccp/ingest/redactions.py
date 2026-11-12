"""Checks on a redaction report sent by the capture client.

The report says *where* rules fired, never *what* was removed. The server cannot verify that, so it
keeps the fields small and well formed: a rule id is a short identifier, a location is a short
string, and the number of records is limited.
"""

from __future__ import annotations

import re
from typing import Any

from ccp.redaction.records import RedactionRecord

MAX_RECORDS = 5000
MAX_LOCATION_LENGTH = 512
MAX_OCCURRENCES = 1_000_000
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class RedactionReportError(ValueError):
    """Raised when a redaction report is malformed. The message is safe to show the client."""


def parse_report(payload: Any) -> list[RedactionRecord]:
    """Validate a decoded JSON report and turn it into records.

    The report is ``{"records": [{"rule_id": ..., "location": ..., "occurrences": ...}, ...]}``;
    ``occurrences`` is optional and defaults to 1.

    Raises:
        RedactionReportError: If the shape is wrong or any field is out of range.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise RedactionReportError('expected {"records": [...]}')
    items = payload["records"]
    if len(items) > MAX_RECORDS:
        raise RedactionReportError(f"too many records (limit {MAX_RECORDS})")

    records = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise RedactionReportError(f"record {number} is not an object")
        rule_id, location = item.get("rule_id"), item.get("location")
        occurrences = item.get("occurrences", 1)
        if not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
            raise RedactionReportError(f"record {number}: invalid rule_id")
        if not isinstance(location, str) or not 0 < len(location) <= MAX_LOCATION_LENGTH:
            raise RedactionReportError(f"record {number}: invalid location")
        if (
            isinstance(occurrences, bool)
            or not isinstance(occurrences, int)
            or not 0 < occurrences <= MAX_OCCURRENCES
        ):
            raise RedactionReportError(f"record {number}: invalid occurrences")
        records.append(RedactionRecord(rule_id, location, occurrences))
    return records
