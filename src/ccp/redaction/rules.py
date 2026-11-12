"""The redaction rules: what counts as a secret or as personal data.

Rules are plain data (:class:`Rule`), so adding or removing one is a one-entry change that is
easy to review. The engine that applies them is in :mod:`ccp.redaction.redactor`.

Two families:

* **Secrets**: credentials in well-known formats, plus generic ``password = ...`` assignments.
* **Personal data** (India): Aadhaar number, PAN, mobile number and email address.

The patterns favour catching too much over too little: a false positive loses a little
context, a false negative leaks data. Where a cheap check exists (the Aadhaar checksum), it
is used to avoid redacting ordinary numbers.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ccp.redaction.checksums import is_valid_verhoeff


@dataclass(frozen=True)
class Rule:
    """One redaction rule.

    Attributes:
        rule_id: Short stable name, written to the redaction log (never the value).
        description: What the rule looks for, in plain words.
        pattern: Regular expression that finds a candidate.
        group: Which part of the match is the sensitive text (0 = the whole match). Using a
            group keeps the surrounding context, for example the ``password =`` before a value.
        validator: Optional extra check on the sensitive text; the candidate is redacted only
            if it returns True.
    """

    rule_id: str
    description: str
    pattern: re.Pattern[str]
    group: int = 0
    validator: Callable[[str], bool] | None = None


def _rule(
    rule_id: str,
    description: str,
    pattern: str,
    *,
    group: int = 0,
    validator: Callable[[str], bool] | None = None,
    flags: int = 0,
) -> Rule:
    """Build a :class:`Rule`, compiling the pattern."""
    return Rule(rule_id, description, re.compile(pattern, flags), group, validator)


# Text already replaced by a redaction must not be matched again by a later rule.
_NOT_REDACTED = r"(?!\[REDACTED:)"

# A digit run that is part of a longer identifier is not a person's number. Without this,
# the last group of a UUID (12 characters) or a run inside a hash would be redacted, which
# would break ids such as `uuid` and `parentUuid` that the indexer depends on. The lookbehinds
# reject a preceding letter or digit, and the last group of a UUID (4 hex characters + "-");
# `_NUMBER_END` rejects a following letter or digit.
_NUMBER_START = r"(?<![A-Za-z0-9])(?<![0-9A-Fa-f]{4}-)"
_NUMBER_END = r"(?![A-Za-z0-9])"


def _is_not_a_git_address(email: str) -> bool:
    """Skip ``git@host`` addresses: they are SSH remotes, not people."""
    return not email.lower().startswith("git@")


# ---------------------------------------------------------------- secrets

SECRET_RULES: tuple[Rule, ...] = (
    _rule(
        "private-key",
        "PEM private key block (also when the end marker is cut off)",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
        flags=re.DOTALL,
    ),
    _rule("aws-access-key-id", "AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _rule("github-token", "GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    _rule("github-fine-grained-token", "GitHub fine-grained token", r"\bgithub_pat_\w{22,}"),
    _rule("slack-token", "Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    _rule("stripe-key", "Stripe API key", r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    _rule("anthropic-api-key", "Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    _rule("openai-style-key", "OpenAI-style secret key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    _rule("google-api-key", "Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    _rule(
        "jwt",
        "JSON Web Token",
        r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    ),
    _rule(
        "url-password",
        "Password inside a URL (scheme://user:password@host)",
        r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s@/]+)@",
        group=1,
    ),
    _rule(
        "bearer-token",
        "Authorization bearer token",
        rf"\bbearer\s+{_NOT_REDACTED}([A-Za-z0-9._~+/=-]{{20,}})",
        group=1,
        flags=re.IGNORECASE,
    ),
    _rule(
        "secret-assignment",
        "Value assigned to a name like password, secret, token or api_key",
        # No leading \b: names such as db_password or AWS_SECRET_ACCESS_KEY must match too
        # (an underscore is a word character, so \b would not fire inside them). A suffix is
        # allowed only after _ or -: secret_key and token_value match; tokenizer does not.
        rf"""(?:api[_-]?key|secret|token|passw(?:or)?d|pwd)(?:[_-][A-Za-z0-9_-]*)?["']?\s*[:=]"""
        rf"""\s*["']?{_NOT_REDACTED}([^\s"',;]{{8,}})""",
        group=1,
        flags=re.IGNORECASE,
    ),
)

# ---------------------------------------------------------------- personal data (India)

PERSONAL_DATA_RULES: tuple[Rule, ...] = (
    _rule(
        "aadhaar",
        "Aadhaar number (12 digits, optionally grouped 4-4-4, passes the Verhoeff check)",
        _NUMBER_START + r"[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}" + _NUMBER_END,
        validator=lambda text: is_valid_verhoeff(re.sub(r"\D", "", text)),
    ),
    _rule(
        "pan",
        "PAN (Permanent Account Number): 5 letters, 4 digits, 1 letter",
        r"\b[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]\b",
    ),
    _rule(
        "indian-mobile",
        "Indian mobile number, with or without +91 / 91 / 0",
        _NUMBER_START + r"(?:(?:\+91|91|0)[ -]?)?[6-9]\d{4}[ -]?\d{5}" + _NUMBER_END,
    ),
    _rule(
        "email",
        "Email address",
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
        validator=_is_not_a_git_address,
    ),
)

# Order matters: specific secret formats first, then generic assignments, then personal data.
DEFAULT_RULES: tuple[Rule, ...] = SECRET_RULES + PERSONAL_DATA_RULES
