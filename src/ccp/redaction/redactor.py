"""The redaction engine: applies rules to text, JSON values and transcript lines.

Guarantees:

* **Values are never reported.** A :class:`Hit` holds the rule id and the *location* (line
  number and JSON path), not the text that was removed.
* **Unchanged lines stay byte-for-byte identical.** A line is only re-written if something
  was redacted, so byte offsets and hashes of clean data are preserved.
* **Fail closed.** If a line cannot be processed, it is withheld, not uploaded as-is.
* **Idempotent.** Redacting already-redacted text changes nothing.

Known limit: only *strings* are scanned. A number stored as a JSON number (for example
``{"phone": 9876543210}``) is not inspected.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ccp.redaction.rules import DEFAULT_RULES, Rule


def placeholder(rule_id: str) -> str:
    """Return the text that replaces a redacted value, for example ``[REDACTED:aadhaar]``."""
    return f"[REDACTED:{rule_id}]"


@dataclass(frozen=True)
class Hit:
    """One redaction. Records where and by which rule, never the removed value.

    Attributes:
        rule_id: The rule that matched.
        line: 1-based line number in the transcript (0 if not from a transcript line).
        path: JSON path of the string that held the value, for example
            ``message.content[0].text``; empty for a plain-text line.
    """

    rule_id: str
    line: int
    path: str


@dataclass
class LineResult:
    """The outcome of redacting one transcript line.

    Attributes:
        text: The line to upload (original text if nothing was redacted), or ``None`` if the
            line was withheld.
        hits: Every redaction made in the line.
        withheld: True if the line could not be processed and must not be uploaded.
    """

    text: str | None
    hits: list[Hit] = field(default_factory=list)
    withheld: bool = False


def redact_string(text: str, rules: Sequence[Rule] = DEFAULT_RULES) -> tuple[str, list[str]]:
    """Redact one string.

    Args:
        text: The text to clean.
        rules: Rules to apply, in order.

    Returns:
        The cleaned text, and the rule id of every redaction made (one entry per hit).
    """
    fired: list[str] = []
    for rule in rules:
        text = _apply_rule(text, rule, fired)
    return text, fired


def _apply_rule(text: str, rule: Rule, fired: list[str]) -> str:
    """Replace every valid match of ``rule`` in ``text``; append its id to ``fired``."""

    def replace(match: re.Match[str]) -> str:
        secret = match.group(rule.group)
        if rule.validator is not None and not rule.validator(secret):
            return match.group(0)  # looks like a candidate but fails the extra check
        fired.append(rule.rule_id)
        # Replace only the sensitive group, keeping the text around it.
        start = match.start(rule.group) - match.start()
        end = match.end(rule.group) - match.start()
        whole = match.group(0)
        return whole[:start] + placeholder(rule.rule_id) + whole[end:]

    return rule.pattern.sub(replace, text)


def redact_value(value: Any, path: str, rules: Sequence[Rule], found: list[tuple[str, str]]) -> Any:
    """Redact every string inside a parsed JSON value, including dictionary keys.

    Args:
        value: Any JSON-compatible value.
        path: JSON path of ``value``, used for the log (empty at the top level).
        rules: Rules to apply.
        found: List that receives a ``(rule_id, path)`` pair for every redaction.

    Returns:
        A cleaned copy of ``value``. Numbers, booleans and null are returned unchanged.
    """
    if isinstance(value, str):
        cleaned, fired = redact_string(value, rules)
        found.extend((rule_id, path) for rule_id in fired)
        return cleaned
    if isinstance(value, list):
        return [redact_value(item, f"{path}[{i}]", rules, found) for i, item in enumerate(value)]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            clean_key, fired = redact_string(key, rules)
            # The path is built from the *cleaned* key: a key that is itself a secret must not
            # end up in the redaction log through the location.
            child_path = f"{path}.{clean_key}" if path else clean_key
            found.extend((rule_id, child_path) for rule_id in fired)
            result[clean_key] = redact_value(item, child_path, rules, found)
        return result
    return value


def redact_line(
    raw_line: str, line_number: int, rules: Sequence[Rule] = DEFAULT_RULES
) -> LineResult:
    """Redact one transcript line (normally a JSON object).

    A line that is not valid JSON (for example a partly written last line) is cleaned as
    plain text. If anything unexpected goes wrong, the line is withheld.

    Args:
        raw_line: The line as read from the transcript, without the trailing newline.
        line_number: 1-based position of the line, for the log.
        rules: Rules to apply.

    Returns:
        The line to upload and the hits, or a withheld result.
    """
    try:
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            return _redact_plain_text(raw_line, line_number, rules)

        found: list[tuple[str, str]] = []
        cleaned = redact_value(parsed, "", rules, found)
        hits = [Hit(rule_id, line_number, path) for rule_id, path in found]
        if not hits:
            return LineResult(text=raw_line)  # untouched: keep the exact original bytes
        # Compact, like Claude Code's own output: a redacted line stays in the same format, which
        # lets the attachment de-duplication (it needs an exact round trip) still process it.
        compact = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        return LineResult(text=compact, hits=hits)
    except Exception:  # noqa: BLE001  (deliberate: any failure must withhold the line)
        return LineResult(text=None, withheld=True)


def _redact_plain_text(raw_line: str, line_number: int, rules: Sequence[Rule]) -> LineResult:
    """Clean a line that is not JSON, as one piece of text."""
    cleaned, fired = redact_string(raw_line, rules)
    hits = [Hit(rule_id, line_number, "") for rule_id in fired]
    return LineResult(text=cleaned if hits else raw_line, hits=hits)
