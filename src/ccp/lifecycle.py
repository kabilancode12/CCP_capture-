"""Session lifecycle events: a chat was opened (start) or closed (end).

A chat (one Claude Code conversation) can be opened several times: started, resumed, continued after
``/compact``. Each opening is a *session* (table ``chat_session``). The capture client reports its
start from the SessionStart hook and its end from the SessionEnd hook, as a small JSON object::

    {"kind": "start", "run_key": "<uuid>", "at": "2026-11-15T17:35:02+00:00",
     "source": "resume", "commit": "<sha or null>"}
    {"kind": "end", "run_key": "<same uuid>", "at": "...", "reason": "prompt_input_exit",
     "commit": "<sha or null>"}

``run_key`` is made by the client at SessionStart and remembered for the chat, so the end carries
the same key and every upload can be repeated safely. Shared by the client (which builds the event)
and the server (which validates it), so it uses the standard library only.

Values observed on Claude Code 2.1.325: start sources ``startup | resume | clear | compact``; end
reasons ``clear | resume | logout | prompt_input_exit | other``. A value Claude Code adds later is
stored as ``unknown`` (start) or ``other`` (end) rather than refused, so no event is lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

START = "start"
END = "end"

START_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
END_REASONS = frozenset({"clear", "resume", "logout", "prompt_input_exit", "other"})
UNKNOWN_SOURCE = "unknown"
UNKNOWN_REASON = "other"

_RUN_KEY = re.compile(r"[A-Za-z0-9-]{8,64}")
_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_KNOWN_KEYS = frozenset({"kind", "run_key", "at", "source", "reason", "commit"})


class LifecycleEventError(ValueError):
    """Raised for a malformed event. The message never repeats the offending value."""


@dataclass(frozen=True)
class LifecycleEvent:
    """One start or end of a session.

    Attributes:
        kind: ``start`` or ``end``.
        run_key: The client's key for this session (the same at start and end).
        at: When it happened (time-zone aware).
        source: For a start: how the chat was opened (see the module description).
        reason: For an end: why it closed.
        commit: The Git HEAD of the working folder at that moment, if known.
    """

    kind: str
    run_key: str
    at: datetime
    source: str | None = None
    reason: str | None = None
    commit: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the event as a JSON-ready dictionary (the API's request body)."""
        payload: dict[str, Any] = {
            "kind": self.kind,
            "run_key": self.run_key,
            "at": self.at.isoformat(),
            "commit": self.commit,
        }
        if self.kind == START:
            payload["source"] = self.source
        else:
            payload["reason"] = self.reason
        return payload


def now() -> datetime:
    """The current time in UTC (the time stamp of a new event)."""
    return datetime.now(UTC)


def parse_lifecycle(payload: Any) -> LifecycleEvent:
    """Validate a decoded JSON payload and turn it into an event.

    Unknown keys are ignored (a newer client may send more). An unknown start source becomes
    ``unknown`` and an unknown end reason ``other``. A time without a time zone is read as UTC.

    Raises:
        LifecycleEventError: If the payload is not an object, the kind or run key is missing or
            malformed, the time is not an ISO 8601 time, or the commit is not a hex object id.
    """
    if not isinstance(payload, dict) or not payload.keys() & _KNOWN_KEYS:
        raise LifecycleEventError("expected a JSON object with kind, run_key and at")
    kind = payload.get("kind")
    if kind not in (START, END):
        raise LifecycleEventError("kind must be 'start' or 'end'")
    run_key = payload.get("run_key")
    if not isinstance(run_key, str) or not _RUN_KEY.fullmatch(run_key):
        raise LifecycleEventError("run_key must be 8 to 64 letters, digits or dashes")
    commit = payload.get("commit")
    if commit is not None and (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)):
        raise LifecycleEventError("commit must be a hex object id or null")
    at = _parse_time(payload.get("at"))
    if kind == START:
        source = payload.get("source")
        known = isinstance(source, str) and source in START_SOURCES
        source = source if known else UNKNOWN_SOURCE
        return LifecycleEvent(START, run_key, at, source=source, commit=commit)
    reason = payload.get("reason")
    reason = reason if isinstance(reason, str) and reason in END_REASONS else UNKNOWN_REASON
    return LifecycleEvent(END, run_key, at, reason=reason, commit=commit)


def _parse_time(value: object) -> datetime:
    """Read an ISO 8601 time; a time without a zone is taken as UTC."""
    if not isinstance(value, str) or len(value) > 64:
        raise LifecycleEventError("at must be an ISO 8601 time")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise LifecycleEventError("at must be an ISO 8601 time") from error
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
