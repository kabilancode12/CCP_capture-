"""The environment record: who ran a session, with which Claude Code, and where.

One record per session (the ``session_env`` table). It is built in two stages on the developer's
machine (see :mod:`ccp.capture.environment`) and sent as partial updates that the server merges:
a field that is present replaces the stored value, a missing field leaves it as it was.

This record is **personal data** (user name, host name, account e-mail). It is collected on
purpose, as the plan says, and is kept even in private mode. Access to it should be restricted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Any

MAX_VALUE_LENGTH = 512
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class EnvironmentRecordError(ValueError):
    """Raised when an environment payload is malformed. The message is safe to show the client."""


@dataclass(frozen=True)
class EnvironmentRecord:
    """The facts about where a session ran. Every field is optional (unknown = ``None``).

    Field names are the column names of ``session_env``. Values marked *observed* were
    confirmed on Claude Code 2.1.289 (see docs/verify/transcript-facts.md).
    """

    claude_code_version: str | None = None  # observed: "version" key of transcript entries
    entrypoint: str | None = None  # observed: cli | claude-vscode (CLAUDE_CODE_ENTRYPOINT)
    surface: str | None = None  # terminal | vscode | jetbrains | desktop | web  [verify]
    run_mode: str | None = None  # interactive | headless | sdk  [verify]
    tool_shell: str | None = None  # shell Claude uses for tools (transcript environment entry)
    hook_shell: str | None = None  # shell the capture hook runs in (for example git-bash)
    os_name: str | None = None
    os_version: str | None = None
    cpu_architecture: str | None = None
    access_path: str | None = None  # local | ssh | container | remote  [verify]
    os_user: str | None = None
    hostname: str | None = None
    account_name: str | None = None
    account_email: str | None = None
    organisation_name: str | None = None
    organisation_uuid: str | None = None
    auth_type: str | None = None  # subscription | api_key

    def to_payload(self) -> dict[str, str]:
        """Return only the fields that are known, as a JSON-ready dictionary."""
        return {f.name: v for f in fields(self) if (v := getattr(self, f.name)) is not None}

    def merged_with(self, other: EnvironmentRecord) -> EnvironmentRecord:
        """Return a record where every field known in ``other`` replaces this one's."""
        merged = {**self.to_payload(), **other.to_payload()}
        return EnvironmentRecord(**merged)


FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(EnvironmentRecord))


def parse_environment(payload: Any) -> EnvironmentRecord:
    """Validate a decoded JSON payload and turn it into a record.

    Raises:
        EnvironmentRecordError: If the payload is not an object, has no fields, has an unknown
            field, or a value is not a short string without control characters. The messages never
            repeat the offending value.
    """
    if not isinstance(payload, dict) or not payload:
        raise EnvironmentRecordError("expected a JSON object with at least one field")
    unknown = [name for name in payload if name not in FIELD_NAMES]
    if unknown:
        raise EnvironmentRecordError(f"{len(unknown)} unknown field(s)")
    for name, value in payload.items():
        if not isinstance(value, str) or not 0 < len(value) <= MAX_VALUE_LENGTH:
            raise EnvironmentRecordError(
                f"field {name}: must be a string of 1 to {MAX_VALUE_LENGTH} characters"
            )
        if _CONTROL_CHARACTERS.search(value):
            raise EnvironmentRecordError(f"field {name}: must not contain control characters")
    return EnvironmentRecord(**payload)
