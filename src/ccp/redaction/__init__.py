"""Redaction: remove secrets and personal data before anything leaves a developer's machine.

Use :func:`redact_line` on each transcript line. It returns the cleaned line and a list of
:class:`Hit` records (rule id and location, never the value) for the redaction log.

Modules:
    rules: the rules themselves (what counts as a secret or personal data).
    redactor: the engine that applies the rules to strings, JSON values and lines.
    checksums: the Verhoeff check used to validate Aadhaar numbers.
"""

from ccp.redaction.redactor import Hit, LineResult, redact_line, redact_string
from ccp.redaction.rules import DEFAULT_RULES, Rule

__all__ = ["DEFAULT_RULES", "Hit", "LineResult", "Rule", "redact_line", "redact_string"]
