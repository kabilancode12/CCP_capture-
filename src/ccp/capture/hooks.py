"""What the capture client does when Claude Code runs a hook.

Claude Code starts ``ccp-capture hook`` for each registered event and sends a JSON payload on
standard input. This module turns the payload into an action:

===================  ==========================================================================
Event                Action
===================  ==========================================================================
SessionStart         record the environment, catch up on earlier sessions, sync this session
UserPromptSubmit     sync the session (short time limit: Claude waits for this hook)
Stop                 sync the session; record the environment once the transcript exists
SubagentStop         sync the session, but not for Claude's internal helpers (empty agent type)
PreCompact           sync the session before it is compacted
SessionEnd           start a detached flush and return at once (best effort, see ``process``)
===================  ==========================================================================

Rules a hook must obey (they come from how Claude Code treats hooks):

* **Never fail and never block.** Every problem is logged and the exit code is still 0 (a hook that
  exits with code 2 can stop Claude from finishing). Time limits are short.
* **Print nothing.** A SessionStart hook's standard output is added to the conversation, so the
  hook must write to the log file only.
* **Log no content.** Only event names, ids, counts and short reasons are written.
* **Be safe to repeat.** SessionStart also runs again after ``/compact`` and on resume, and Stop
  does not run after a turn ended by a permission denial: every action is idempotent, and the next
  hook (or the next catch-up) picks up what an earlier one missed.

Private mode (sending only a minimal record) is not implemented yet.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ccp.capture.client import StreamResult
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord

logger = logging.getLogger("ccp.capture.hook")

SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
STOP = "Stop"
SUBAGENT_STOP = "SubagentStop"
PRE_COMPACT = "PreCompact"
SESSION_END = "SessionEnd"

# The events the client registers for, with the time limit (seconds) given to Claude Code for each.
HOOK_TIMEOUTS = {
    SESSION_START: 30,
    USER_PROMPT_SUBMIT: 15,
    STOP: 30,
    SUBAGENT_STOP: 30,
    PRE_COMPACT: 30,
    SESSION_END: 5,
}

# How long each hook may spend uploading. Hooks that hold Claude up (start, prompt) get less.
START_BUDGET_SECONDS = 8.0
PROMPT_BUDGET_SECONDS = 5.0
STOP_BUDGET_SECONDS = 15.0  # the code snapshot that follows the upload needs time too
SUBAGENT_BUDGET_SECONDS = 15.0
COMPACT_BUDGET_SECONDS = 10.0

ENVIRONMENT_COMPLETE_MARKER = "environment-complete"


@dataclass(frozen=True)
class HookPayload:
    """The parts of a hook payload the client uses.

    Attributes:
        event: The hook event name (``hook_event_name``).
        session_id: Claude Code's session id.
        transcript: The session transcript, if the payload names a ``.jsonl`` file.
        agent_type: For SubagentStop: the agent type (empty for Claude's internal helpers).
        source: For SessionStart: ``startup``, ``resume``, ``clear`` or ``compact``.
        cwd: The directory Claude Code works in (the repository to snapshot).
    """

    event: str
    session_id: str
    transcript: Path | None
    agent_type: str | None
    source: str | None
    cwd: Path | None = None


def parse_payload(text: str) -> HookPayload | None:
    """Read a hook payload from JSON text.

    Returns:
        The payload, or ``None`` if it is not a JSON object with an event name and a session id.
        Only a path ending in ``.jsonl`` is accepted as the transcript.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    event, session_id = data.get("hook_event_name"), data.get("session_id")
    if not isinstance(event, str) or not isinstance(session_id, str) or not session_id:
        return None
    raw_path = data.get("transcript_path")
    transcript = (
        Path(raw_path) if isinstance(raw_path, str) and raw_path.endswith(".jsonl") else None
    )
    agent_type, source, cwd = data.get("agent_type"), data.get("source"), data.get("cwd")
    return HookPayload(
        event=event,
        session_id=session_id,
        transcript=transcript,
        agent_type=agent_type if isinstance(agent_type, str) else None,
        source=source if isinstance(source, str) else None,
        cwd=Path(cwd) if isinstance(cwd, str) and cwd else None,
    )


class ClientPort(Protocol):
    """What the handler needs from the capture client."""

    def sync_session(
        self, session_id: str, transcript_path: Path, budget_seconds: float | None = None
    ) -> list[StreamResult]:
        """Upload what is new for a session."""

    def catch_up(self, budget_seconds: float | None = None) -> dict[str, list[StreamResult]]:
        """Upload what earlier sessions left behind."""

    def report_environment(self, session_id: str, record: EnvironmentRecord) -> str | None:
        """Send the environment record; return a reason if it could not be sent."""


class SnapshotPort(Protocol):
    """What the handler needs from the code-snapshot service (``None`` result = disabled)."""

    def start(self, session_id: str, cwd: Path) -> str | None:
        """At session start: retry queued pushes, baseline snapshot, branch backup."""

    def snapshot(self, session_id: str, cwd: Path, event: str) -> str | None:
        """After a response: snapshot the working tree if it changed, and push it."""


class HookHandler:
    """Carries out the action for each hook event."""

    def __init__(
        self,
        client: ClientPort,
        state: StateStore,
        collect_environment: Callable[[Path | None], EnvironmentRecord],
        start_flush: Callable[[str, Path, Path | None], None],
        snapshots: SnapshotPort | None = None,
    ) -> None:
        """Create the handler.

        Args:
            client: Uploads transcripts and the environment record.
            state: The client's small local state (used to remember that the environment is done).
            collect_environment: Builds the environment record; given the transcript path once it
                exists, or ``None`` before.
            start_flush: Starts a detached upload of a session (used at SessionEnd); given the
                session id, the transcript and the working directory (for the final snapshot).
            snapshots: Takes code snapshots; ``None`` means the feature is not wired in.
        """
        self._client = client
        self._state = state
        self._collect_environment = collect_environment
        self._start_flush = start_flush
        self._snapshots = snapshots

    def handle(self, payload: HookPayload) -> str:
        """Do what the event calls for.

        Returns:
            A short description for the log: counts and reasons, never content.
        """
        event = payload.event
        if event == SESSION_START:
            return self._session_start(payload)
        if event == USER_PROMPT_SUBMIT:
            return self._sync(payload, PROMPT_BUDGET_SECONDS)
        if event == STOP:
            return self._stop(payload)
        if event == PRE_COMPACT:
            return self._sync(payload, COMPACT_BUDGET_SECONDS)
        if event == SUBAGENT_STOP:
            if not payload.agent_type:
                return "skipped: internal helper (no agent type)"
            return self._sync(payload, SUBAGENT_BUDGET_SECONDS)
        if event == SESSION_END:
            return self._session_end(payload)
        return f"ignored: not an event this client handles ({event})"

    # ------------------------------------------------------------ events

    def _session_start(self, payload: HookPayload) -> str:
        """Record the environment, catch up on earlier sessions, then sync this one."""
        parts = [f"source={payload.source or '?'}"]
        parts.append("environment: " + self._report_environment(payload))
        caught_up = self._client.catch_up(START_BUDGET_SECONDS)
        parts.append(f"caught up {len(caught_up)} earlier session(s)")
        if self._transcript_exists(payload):  # a resumed or compacted session already has a file
            parts.append(self._sync(payload, START_BUDGET_SECONDS))
        if self._snapshots is not None and payload.cwd is not None:
            note = self._snapshots.start(payload.session_id, payload.cwd)
            if note is not None:  # None means snapshots are switched off: say nothing
                parts.append(f"code: {note}")
        return "; ".join(parts)

    def _stop(self, payload: HookPayload) -> str:
        """After a response: upload first (it matters most), then snapshot the code."""
        summary = self._sync(payload, STOP_BUDGET_SECONDS)
        if self._snapshots is not None and payload.cwd is not None:
            note = self._snapshots.snapshot(payload.session_id, payload.cwd, STOP)
            if note is not None:
                summary += f"; code: {note}"
        return summary

    def _sync(self, payload: HookPayload, budget_seconds: float) -> str:
        """Upload the session's new lines, then complete the environment record."""
        if payload.transcript is None or not payload.transcript.is_file():
            return "no transcript yet"
        results = self._client.sync_session(payload.session_id, payload.transcript, budget_seconds)
        summary = _describe(results)
        return f"{summary}; environment: {self._report_environment(payload)}"

    def _session_end(self, payload: HookPayload) -> str:
        """Start a detached flush; never wait for it (SessionEnd has almost no time)."""
        if not self._transcript_exists(payload) or payload.transcript is None:
            return "no transcript to flush"
        try:
            self._start_flush(payload.session_id, payload.transcript, payload.cwd)
        except OSError as error:
            return f"flush could not be started ({type(error).__name__})"
        return "flush started in the background"

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _transcript_exists(payload: HookPayload) -> bool:
        return payload.transcript is not None and payload.transcript.is_file()

    def _report_environment(self, payload: HookPayload) -> str:
        """Send the environment record until it is complete (it has the Claude Code version)."""
        if self._state.get_marker(payload.session_id, ENVIRONMENT_COMPLETE_MARKER):
            return "already recorded"
        transcript = payload.transcript if self._transcript_exists(payload) else None
        record = self._collect_environment(transcript)
        problem = self._client.report_environment(payload.session_id, record)
        if problem is not None:
            return f"not sent ({problem})"
        if record.claude_code_version:  # the transcript stage is done: nothing more to collect
            self._state.set_marker(payload.session_id, ENVIRONMENT_COMPLETE_MARKER, "1")
        return "sent"


def _describe(results: list[StreamResult]) -> str:
    """Summarise stream results for the log: counts and reasons only."""
    chunks = sum(r.chunks for r in results)
    attachments = sum(r.attachments for r in results)
    errors = [f"{r.stream}: {r.error}" for r in results if r.error]
    text = f"{chunks} chunk(s), {attachments} attachment(s) uploaded"
    return text + ("; " + "; ".join(errors) if errors else "")
