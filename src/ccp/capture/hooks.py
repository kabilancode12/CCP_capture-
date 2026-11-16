"""What the capture client does when Claude Code runs a hook.

Claude Code starts ``ccp-capture hook`` for each registered event and sends a JSON payload on
standard input. This module turns the payload into an action:

===================  ==========================================================================
Event                Action
===================  ==========================================================================
SessionStart         queue the session start, record the environment, catch up on earlier
                     sessions (this sends the start too), sync this session
UserPromptSubmit     sync the session without its tool-result files (short time limit: Claude
                     waits for this hook)
Stop                 sync the session; record the environment once the transcript exists
SubagentStop         sync the session, but not for Claude's internal helpers (empty agent type)
PreCompact           sync the session before it is compacted
SessionEnd           queue the session end, start a detached flush that sends it and uploads the
                     rest, and return at once (best effort, see ``process``)
===================  ==========================================================================

Session start and end (see :mod:`ccp.lifecycle`): SessionStart makes a new *run key* for this
opening of the chat and remembers it in the state folder; SessionEnd sends the same key, so the
server can pair them. SessionStart only queues the start: the catch-up that follows sends it with
the waiting events of other chats, so that an unreachable API costs the hook one timeout, not one
per request (the client also fails fast, see :mod:`ccp.capture.api`). An end without a remembered
key gets a fresh one. Both carry the Git HEAD of the working folder: SessionStart reads it with a
short time limit; SessionEnd does not wait for Git at all (it may be stopped at any moment), and the
detached flush adds the HEAD to the end before sending it (``ccp-capture flush``).

The environment record is sent until it is complete. If the API is older and takes only the older
fields, the whole record is sent again an hour later (it may have been upgraded by then).

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
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ccp.capture.client import ENVIRONMENT_PARTIAL, UNSUPPORTED_RETRY_SECONDS, StreamResult
from ccp.capture.snapshots.context import (
    AI_OTHER,
    HUMAN,
    SnapshotContext,
    actor_of_tool,
    read_transcript_context,
    target_of,
)
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.lifecycle import END, START, LifecycleEvent, now

logger = logging.getLogger("ccp.capture.hook")

SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
STOP = "Stop"
SUBAGENT_STOP = "SubagentStop"
PRE_COMPACT = "PreCompact"
SESSION_END = "SessionEnd"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"

# The events the client registers for, with the time limit (seconds) given to Claude Code for each.
HOOK_TIMEOUTS = {
    SESSION_START: 30,
    USER_PROMPT_SUBMIT: 15,
    STOP: 30,
    SUBAGENT_STOP: 30,
    PRE_COMPACT: 30,
    SESSION_END: 5,
    PRE_TOOL_USE: 5,
    POST_TOOL_USE: 20,
}

# The tools whose calls may change files: only these get a hook (a read-only tool needs none).
TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell"
HOOK_MATCHERS = {PRE_TOOL_USE: TOOL_MATCHER, POST_TOOL_USE: TOOL_MATCHER}
TOOL_BUSY_MARKER = "tool-busy"  # set while a tool runs, so the watcher leaves its changes alone

# How long each hook may spend uploading. Hooks that hold Claude up (start, prompt) get less.
START_BUDGET_SECONDS = 8.0
PROMPT_BUDGET_SECONDS = 5.0
STOP_BUDGET_SECONDS = 15.0  # the code snapshot that follows the upload needs time too
SUBAGENT_BUDGET_SECONDS = 15.0
COMPACT_BUDGET_SECONDS = 10.0

# Time limit for reading the Git HEAD at SessionStart (SessionEnd does not read it; see above).
START_COMMIT_TIMEOUT_SECONDS = 2.0

# The record is complete ("1"), or was taken in part by an older API ("partial <seconds since
# 1970>"). Named anew in migration 016, so that records an older API took in part before are sent
# again once.
ENVIRONMENT_COMPLETE_MARKER = "environment-done"
RUN_KEY_MARKER = "run-key"


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
        reason: For SessionEnd: ``clear``, ``resume``, ``logout``, ``prompt_input_exit`` or
            ``other``.
    """

    event: str
    session_id: str
    transcript: Path | None
    agent_type: str | None
    source: str | None
    cwd: Path | None = None
    reason: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: object = None


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
    reason = data.get("reason")
    tool_name, tool_use_id = data.get("tool_name"), data.get("tool_use_id")
    return HookPayload(
        event=event,
        session_id=session_id,
        transcript=transcript,
        agent_type=agent_type if isinstance(agent_type, str) else None,
        source=source if isinstance(source, str) else None,
        cwd=Path(cwd) if isinstance(cwd, str) and cwd else None,
        reason=reason if isinstance(reason, str) else None,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
        tool_input=data.get("tool_input"),
    )


class ClientPort(Protocol):
    """What the handler needs from the capture client."""

    def sync_session(
        self,
        session_id: str,
        transcript_path: Path,
        budget_seconds: float | None = None,
        *,
        tool_results: bool = True,
    ) -> list[StreamResult]:
        """Upload what is new for a session (its tool-result files only if ``tool_results``)."""

    def catch_up(self, budget_seconds: float | None = None) -> dict[str, list[StreamResult]]:
        """Upload what earlier sessions left behind."""

    def report_environment(self, session_id: str, record: EnvironmentRecord) -> str | None:
        """Send the environment record; return a reason if it could not be sent."""

    def queue_lifecycle(self, session_id: str, event: LifecycleEvent) -> None:
        """Keep a session start or end until it is sent (by the catch-up, a sync or the flush)."""


class SnapshotPort(Protocol):
    """What the handler needs from the code-snapshot service (``None`` result = disabled)."""

    def start(
        self, session_id: str, cwd: Path, context: SnapshotContext | None = None
    ) -> str | None:
        """At session start: retry queued pushes, baseline snapshot, branch backup."""

    def snapshot(
        self, session_id: str, cwd: Path, event: str, context: SnapshotContext | None = None
    ) -> str | None:
        """After a response: snapshot the working tree if it changed, and push it."""


class HookHandler:
    """Carries out the action for each hook event."""

    def __init__(
        self,
        client: ClientPort,
        state: StateStore,
        collect_environment: Callable[[Path | None, Path | None], EnvironmentRecord],
        start_flush: Callable[[str, Path | None, Path | None], None],
        snapshots: SnapshotPort | None = None,
        read_commit: Callable[[Path, float], str | None] | None = None,
        clock: Callable[[], datetime] = now,
    ) -> None:
        """Create the handler.

        Args:
            client: Uploads transcripts, the environment record and lifecycle events.
            state: The client's small local state (the environment is done, the run key).
            collect_environment: Builds the environment record; given the transcript path once it
                exists (``None`` before) and the working directory (``None`` if unknown).
            start_flush: Starts a detached upload of a session (used at SessionEnd); given the
                session id, the transcript (``None`` if there is none: the flush then only sends
                the waiting lifecycle events) and the working directory (for the final snapshot).
            snapshots: Takes code snapshots; ``None`` means the feature is not wired in.
            read_commit: Returns the Git HEAD of a folder within a time limit (seconds), or
                ``None``; ``None`` means commits are not recorded.
            clock: Returns the current time (replaced in tests).
        """
        self._client = client
        self._state = state
        self._collect_environment = collect_environment
        self._start_flush = start_flush
        self._snapshots = snapshots
        self._read_commit = read_commit
        self._clock = clock

    def handle(self, payload: HookPayload) -> str:
        """Do what the event calls for.

        Returns:
            A short description for the log: counts and reasons, never content.
        """
        event = payload.event
        if event == SESSION_START:
            return self._session_start(payload)
        if event == PRE_TOOL_USE:
            self._state.set_marker(payload.session_id, TOOL_BUSY_MARKER, str(time.time()))
            return "tool started"
        if event == POST_TOOL_USE:
            return self._after_tool(payload)
        if event == USER_PROMPT_SUBMIT:  # tool results wait for Stop: one file could hold Claude up
            summary = self._sync(payload, PROMPT_BUDGET_SECONDS, tool_results=False)
            # Whatever changed since the last snapshot was not done by Claude: a person did it.
            return summary + self._code_note(payload, USER_PROMPT_SUBMIT, SnapshotContext(HUMAN))
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
        """Queue the start, record the environment, catch up on earlier sessions, sync this one.

        The start is not sent here on its own: the catch-up sends it (and the next sync, if the
        catch-up ran out of time).
        """
        parts = [f"source={payload.source or '?'}"]
        run_key = str(uuid.uuid4())
        self._state.set_marker(payload.session_id, RUN_KEY_MARKER, run_key)
        start = LifecycleEvent(
            START,
            run_key,
            self._clock(),
            source=payload.source,
            commit=self._commit(payload, START_COMMIT_TIMEOUT_SECONDS),
        )
        self._client.queue_lifecycle(payload.session_id, start)
        parts.append("start queued")
        parts.append("environment: " + self._report_environment(payload))
        caught_up = self._client.catch_up(START_BUDGET_SECONDS)
        parts.append(f"caught up {len(caught_up)} earlier session(s)")
        if self._transcript_exists(payload):  # a resumed or compacted session already has a file
            parts.append(self._sync(payload, START_BUDGET_SECONDS))
        if self._snapshots is not None and payload.cwd is not None:
            note = self._snapshots.start(payload.session_id, payload.cwd, SnapshotContext(HUMAN))
            if note is not None:  # None means snapshots are switched off: say nothing
                parts.append(f"code: {note}")
        return "; ".join(parts)

    def _stop(self, payload: HookPayload) -> str:
        """After a response: upload first (it matters most), then snapshot the code."""
        summary = self._sync(payload, STOP_BUDGET_SECONDS)
        if self._snapshots is not None and payload.cwd is not None:
            context = self._tool_context(payload, AI_OTHER, None)
            note = self._snapshots.snapshot(payload.session_id, payload.cwd, STOP, context)
            if note is not None:
                summary += f"; code: {note}"
        return summary

    def _after_tool(self, payload: HookPayload) -> str:
        """After a tool call: one snapshot of what it changed, labelled with who and why."""
        self._state.remove_marker(payload.session_id, TOOL_BUSY_MARKER)
        context = self._tool_context(
            payload, actor_of_tool(payload.tool_name, payload.agent_type), payload.tool_name
        )
        return self._code_note(payload, POST_TOOL_USE, context) or "code: snapshots are off"

    def _tool_context(self, payload: HookPayload, actor: str, tool: str | None) -> SnapshotContext:
        """What a snapshot should say: the tool, its call id, the model and the prompt behind it."""
        model, prompt_id, excerpt = read_transcript_context(payload.transcript)
        return SnapshotContext(
            actor=actor,
            model=model,
            tool=tool,
            target=target_of(payload.tool_input),
            tool_use_id=payload.tool_use_id,
            prompt_id=prompt_id,
            prompt_excerpt=excerpt,
            agent=payload.agent_type or "main",
        )

    def _code_note(self, payload: HookPayload, event: str, context: SnapshotContext) -> str:
        """Take a snapshot for the log line; empty when snapshots are not wired in or off."""
        if self._snapshots is None or payload.cwd is None:
            return ""
        note = self._snapshots.snapshot(payload.session_id, payload.cwd, event, context)
        return "" if note is None else f"; code: {note}"

    def _sync(
        self, payload: HookPayload, budget_seconds: float, *, tool_results: bool = True
    ) -> str:
        """Upload the session's new lines, then complete the environment record."""
        if payload.transcript is None or not payload.transcript.is_file():
            return "no transcript yet"
        results = self._client.sync_session(
            payload.session_id, payload.transcript, budget_seconds, tool_results=tool_results
        )
        summary = _describe(results)
        return f"{summary}; environment: {self._report_environment(payload)}"

    def _session_end(self, payload: HookPayload) -> str:
        """Queue the end and start a detached flush; never wait for it (almost no time here).

        The flush adds the Git HEAD to the end, sends it and uploads the rest of the transcript. If
        it never runs, the next catch-up sends the end (without the commit). A chat without a
        transcript (opened and closed without a message) still has its end sent.
        """
        run_key = self._state.get_marker(payload.session_id, RUN_KEY_MARKER) or str(uuid.uuid4())
        end = LifecycleEvent(END, run_key, self._clock(), reason=payload.reason)
        self._client.queue_lifecycle(payload.session_id, end)
        self._state.remove_marker(payload.session_id, RUN_KEY_MARKER)
        transcript = payload.transcript if self._transcript_exists(payload) else None
        try:
            self._start_flush(payload.session_id, transcript, payload.cwd)
        except OSError as error:
            return f"end queued; flush could not be started ({type(error).__name__})"
        if transcript is None:
            return "end queued; no transcript; flush started in the background"
        return "end queued; flush started in the background"

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _transcript_exists(payload: HookPayload) -> bool:
        return payload.transcript is not None and payload.transcript.is_file()

    def _commit(self, payload: HookPayload, timeout: float) -> str | None:
        """Return the Git HEAD of the working folder, or ``None`` if unknown or not recorded."""
        if self._read_commit is None or payload.cwd is None:
            return None
        return self._read_commit(payload.cwd, timeout)

    def _report_environment(self, payload: HookPayload) -> str:
        """Send the environment record until it is complete (it has the Claude Code version).

        A complete record that an older API took only in part is sent again after an hour.
        """
        done = self._state.get_marker(payload.session_id, ENVIRONMENT_COMPLETE_MARKER)
        if done == "1":
            return "already recorded"
        if done is not None and not self._partial_is_old(done):
            return "recorded in part (the API is older); sent again later"
        transcript = payload.transcript if self._transcript_exists(payload) else None
        record = self._collect_environment(transcript, payload.cwd)
        problem = self._client.report_environment(payload.session_id, record)
        if problem == ENVIRONMENT_PARTIAL:
            if record.claude_code_version:
                stamp = f"partial {self._clock().timestamp():.0f}"
                self._state.set_marker(payload.session_id, ENVIRONMENT_COMPLETE_MARKER, stamp)
            return "sent in part (the API is older)"
        if problem is not None:
            return f"not sent ({problem})"
        if record.claude_code_version:  # the transcript stage is done: nothing more to collect
            self._state.set_marker(payload.session_id, ENVIRONMENT_COMPLETE_MARKER, "1")
        return "sent"

    def _partial_is_old(self, marker: str) -> bool:
        """True if a record taken in part (marker ``partial <seconds>``) is due to be sent again."""
        try:
            sent_at = float(marker.removeprefix("partial "))
        except ValueError:
            return True  # unreadable: send again
        return self._clock().timestamp() - sent_at >= UNSUPPORTED_RETRY_SECONDS


def _describe(results: list[StreamResult]) -> str:
    """Summarise stream results for the log: counts and reasons only."""
    chunks = sum(r.chunks for r in results)
    attachments = sum(r.attachments for r in results)
    files = sum(r.files for r in results)
    errors = [f"{r.stream}: {r.error}" for r in results if r.error]
    text = f"{chunks} chunk(s), {attachments} attachment(s)"
    text += f", {files} tool result(s) uploaded" if files else " uploaded"
    return text + ("; " + "; ".join(errors) if errors else "")
