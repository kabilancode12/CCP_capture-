"""The capture client: uploads new transcript lines to the ingestion API.

How a sync works, for one stream (a session transcript or one sub-agent):

1. Take the stream's lock, so two hooks cannot upload it at the same time.
2. Read the complete lines after the saved position and group them into chunks.
3. For each chunk, in order: clean it (redaction, attachment de-duplication), upload its
   attachments, upload the chunk, report its redactions, and **only then** save the new position.

Nothing is lost when the API is down or the machine is offline: the position does not move, the
transcript file keeps the data, and the next sync repeats the same steps. Every step is safe to
repeat (the server answers "already stored"), so a failure halfway is harmless.

When the API refuses an upload:

* 401 or 403 means the token is not accepted. Every other upload would be refused the same way,
  so the run stops (no other stream or session is tried) and the reason is logged.
* Any other refusal (such as 413 or 422) is counted per stream and position. After
  :data:`MAX_REFUSALS` refusals in a row of the same chunk, a ``ccp-withheld`` placeholder is
  uploaded in its place and the position moves on, so one bad chunk cannot block a stream forever.

Sub-agent files are found in ``<session>/subagents/`` and, for sub-agents started by the Workflow
tool, in ``<session>/subagents/workflows/<run id>/``. Each is its own stream, ``agent-<agent id>``
(agent ids are unique within a session). The description (``.meta.json``) of a Workflow sub-agent
does not name its run, so the client adds the run id (the folder name) as ``ccpWorkflowRunId``.

Besides the transcripts, a sync also sends:

* **Lifecycle events** (a session of the chat started or ended, see :mod:`ccp.lifecycle`). The
  hooks queue them in the state folder; every sync and every catch-up sends what is waiting, so an
  event is not lost when the API is down.
* **Tool-result files**: large tool outputs that Claude Code keeps in ``<session>/tool-results/``
  and only refers to from the transcript. Each file is redacted like a transcript (a file that holds
  JSON is decoded first, as transcript lines are) and uploaded once (again if it changes; the server
  keeps both versions). Where redaction rules fired is reported against the object key the server
  stored the content under. If that report fails after the file was stored, the file is sent again
  at the next sync; the server stores nothing new and records each redaction site once, so nothing
  is counted twice. A hook that holds Claude up (UserPromptSubmit) skips them: one large file could
  take longer than its time limit, which is checked only between files.

Both use API endpoints that an older server does not have. Such a server answers 404 or 405: the
client logs it once, waits an hour before asking again (per session), and carries on. These answers
never count as a refused chunk. An older server also refuses the newer environment fields (422): the
record is then sent with the older fields only, and :meth:`CaptureClient.report_environment` says
so (:data:`ENVIRONMENT_PARTIAL`), so that the caller sends the whole record again later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ccp.capture.api import ApiPort, ApiRejectedError, ApiUnavailableError, ToolResultReply
from ccp.capture.pipeline import PreparedChunk, prepare_chunk, refused_chunk
from ccp.capture.reader import SourceLine, read_complete_lines, split_into_chunks
from ccp.capture.state import StateStore
from ccp.environment import LEGACY_FIELD_NAMES, EnvironmentRecord
from ccp.lifecycle import END, START, LifecycleEvent, LifecycleEventError, parse_lifecycle
from ccp.redaction import DEFAULT_RULES, Hit, redact_string
from ccp.redaction.records import records_from_hits
from ccp.redaction.redactor import redact_value
from ccp.storage.keys import (
    MAIN_STREAM,
    TOOL_RESULTS_FOLDER,
    check_id,
    tool_result_key,
    transcript_chunk_key,
)

logger = logging.getLogger("ccp.capture")

# The notes :meth:`CaptureClient.send_lifecycle` returns when the API cannot be reached, and when it
# refuses the token. Either stops a catch-up: every other request would fail the same way.
API_UNAVAILABLE = "the API is unavailable; will retry on the next sync"
TOKEN_REFUSED = "lifecycle events wait (token refused; check CCP_INGEST_TOKEN)"  # noqa: S105

# What :meth:`CaptureClient.report_environment` returns when the API took only the older fields.
ENVIRONMENT_PARTIAL = (
    "sent without the newer fields (the API is older); send the record again later"
)

# The state marker that holds a hash of the environment record last sent. (Named anew in migration
# 016: the earlier marker also covered records of which the API had taken only the older fields.)
ENVIRONMENT_SENT_MARKER = "environment-sent"

DEFAULT_CHUNK_BYTES = 1024 * 1024
DEFAULT_BUDGET_SECONDS = 20.0
DEFAULT_LOCK_WAIT_SECONDS = 10.0

# A chunk refused this many times in a row is replaced by a placeholder.
MAX_REFUSALS = 3

# Refusals that mean the token is not accepted: every request would be refused the same way.
ACCESS_DENIED_STATUSES = frozenset({401, 403})

# Answers of a server that does not have an endpoint yet (it is older than this client).
NOT_SUPPORTED_STATUSES = frozenset({404, 405})

# How long to wait before asking an older server again for an endpoint it did not have (seconds).
UNSUPPORTED_RETRY_SECONDS = 3600.0

# Largest tool-result file uploaded (the server's default upload limit, CCP_MAX_CHUNK_BYTES).
MAX_TOOL_RESULT_BYTES = 8 * 1024 * 1024

# Most lifecycle events kept waiting per session; the oldest are dropped beyond that.
MAX_PENDING_LIFECYCLE = 50

LIFECYCLE_PREFIX = "lifecycle-"
TOOL_RESULT_PREFIX = "tool-result-"
WORKFLOW_RUN_KEY = "ccpWorkflowRunId"


class _Step(Enum):
    """What happened to one tool-result file."""

    DONE = "done"  # uploaded, or given up for good: remember it
    LATER = "later"  # try again at the next sync
    STOP = "stop"  # stop this sync (API down, token refused, or endpoint missing)


@dataclass
class StreamResult:
    """The outcome of syncing one stream.

    Attributes:
        stream: The stream name (``main``, ``agent-<id>``, ``agent-<id>.meta``).
        chunks: How many chunks were uploaded.
        attachments: How many attachment bodies were sent.
        caught_up: True if everything complete in the file has been uploaded.
        error: Why the sync stopped early, or ``None``. It never contains transcript content.
        api_unavailable: True if the stop was because the API could not be reached, so trying other
            streams or sessions right now is pointless.
        access_denied: True if the stop was because the API refused the token (401 or 403), so
            every other stream and session would be refused too.
        skipped: How many chunks were replaced by a placeholder because the API kept refusing them.
        files: How many tool-result files were uploaded (the ``tool-results`` result only).
    """

    stream: str
    chunks: int = 0
    attachments: int = 0
    caught_up: bool = False
    error: str | None = None
    api_unavailable: bool = False
    access_denied: bool = False
    skipped: int = 0
    files: int = 0

    @property
    def stops_run(self) -> bool:
        """True if no other stream or session should be tried in this run."""
        return self.api_unavailable or self.access_denied


class CaptureClient:
    """Uploads transcripts through an :class:`~ccp.capture.api.ApiPort`."""

    def __init__(
        self,
        api: ApiPort,
        state: StateStore,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the client.

        Args:
            api: The ingestion API.
            state: Where the uploaded positions are remembered.
            chunk_bytes: Target size of a chunk, in source bytes.
            budget_seconds: Time one :meth:`sync_session` may take before it stops cleanly (hooks
                have a time limit; the next sync continues where this one stopped).
            lock_wait_seconds: How long to wait for another sync of the same stream to finish
                before skipping it.
            clock: A function returning seconds, for time limits (replaced in tests).
            wall_clock: A function returning the time of day in seconds, for waits that outlive
                one process (replaced in tests).
        """
        self._api = api
        self._state = state
        self._chunk_bytes = chunk_bytes
        self._budget = budget_seconds
        self._lock_wait = lock_wait_seconds
        self._clock = clock
        self._wall_clock = wall_clock

    # ------------------------------------------------------------ public

    def sync_session(
        self,
        session_id: str,
        transcript_path: Path,
        budget_seconds: float | None = None,
        *,
        tool_results: bool = True,
    ) -> list[StreamResult]:
        """Upload everything new for a session: its transcript and its sub-agents' files.

        Args:
            session_id: Claude Code's session id.
            transcript_path: The session transcript (``<session>.jsonl``). Sub-agent files are
                found next to it in ``<session>/subagents/``.
            budget_seconds: Time limit for this call instead of the client's default (a hook that
                holds up Claude Code, such as UserPromptSubmit, uses a short one).
            tool_results: False to leave the tool-result files for a later sync (see the module
                description).

        Returns:
            One result per stream that was tried. If the API is unavailable or refuses the token,
            the remaining streams are not tried (they would fail the same way, and slowly).
        """
        deadline = self._clock() + (self._budget if budget_seconds is None else budget_seconds)
        return self._sync_session_until(session_id, transcript_path, deadline, tool_results)

    def catch_up(self, budget_seconds: float | None = None) -> dict[str, list[StreamResult]]:
        """Sync every session the client has seen whose transcript file still exists.

        Used at session start to pick up what earlier sessions left behind (a session that ended
        abruptly, an upload that failed). It shares one time limit across all sessions and stops
        at the first "API unavailable" or "token refused".

        Args:
            budget_seconds: Time limit for the whole catch-up instead of the client's default.
        """
        deadline = self._clock() + (self._budget if budget_seconds is None else budget_seconds)
        results: dict[str, list[StreamResult]] = {}
        # Waiting session starts and ends first: chats without a transcript (empty chats) have only
        # these. If the API is down or refuses the token, the transcripts would fail too.
        for session_id in self._state.sessions_with_markers(LIFECYCLE_PREFIX):
            if self._clock() >= deadline:
                break
            if self.send_lifecycle(session_id) in (API_UNAVAILABLE, TOKEN_REFUSED):
                return results
        for session_id, path in self._state.known_sessions():
            if self._clock() >= deadline:
                break
            if path.is_file():
                results[session_id] = self._sync_session_until(session_id, path, deadline)
                if any(r.stops_run for r in results[session_id]):
                    break  # the API is down or refuses us: the other sessions would fail too
        return results

    def _sync_session_until(
        self, session_id: str, transcript_path: Path, deadline: float, tool_results: bool = True
    ) -> list[StreamResult]:
        """Sync a session's streams until ``deadline`` (see :meth:`sync_session`)."""
        self._state.remember_transcript(session_id, transcript_path)
        results = [self._sync_lines(session_id, MAIN_STREAM, transcript_path, deadline)]
        if results[0].stops_run:
            return results
        self.send_lifecycle(session_id)

        agents_folder = transcript_path.with_suffix("") / "subagents"
        if agents_folder.is_dir():
            for stream, path in _agent_files(agents_folder, ".jsonl").items():
                results.append(self._sync_lines(session_id, stream, path, deadline))
                if results[-1].stops_run:
                    return results
            for stream, path in _agent_files(agents_folder, ".meta.json").items():
                results.append(self._sync_meta(session_id, stream, path, deadline))
                if results[-1].stops_run:
                    return results

        folder = transcript_path.with_suffix("") / TOOL_RESULTS_FOLDER
        if tool_results and folder.is_dir():
            results.append(self._sync_tool_results(session_id, folder, deadline))
        return results

    def report_environment(self, session_id: str, record: EnvironmentRecord) -> str | None:
        """Send the session's environment record, unless the server already has this exact record.

        The record is sent only if it differs from what was last sent successfully (remembered as
        a hash), so calling this at every hook costs nothing once the record is complete.

        Args:
            session_id: Claude Code's session id.
            record: The known facts (see :mod:`ccp.capture.environment`).

        Returns:
            ``None`` on success (including "nothing to send"); :data:`ENVIRONMENT_PARTIAL` if the
            API took only the older fields (the hash remembered is that of what was sent, so the
            whole record goes out again at the next call); otherwise a short reason that never
            contains the record's values.
        """
        payload = record.to_payload()
        if not payload:
            return None
        if self._state.get_marker(session_id, ENVIRONMENT_SENT_MARKER) == _digest(payload):
            return None  # already sent
        try:
            sent = self._put_environment(session_id, record)
        except ApiUnavailableError as error:
            logger.warning("environment not sent: %s", error)
            return "the API is unavailable; will retry on the next sync"
        except ApiRejectedError as error:
            logger.error("environment refused: %s", error)
            return str(error)
        self._state.set_marker(session_id, ENVIRONMENT_SENT_MARKER, _digest(sent.to_payload()))
        return None if sent == record else ENVIRONMENT_PARTIAL

    def _put_environment(self, session_id: str, record: EnvironmentRecord) -> EnvironmentRecord:
        """Send the record; if the server refuses it as malformed, send the older fields only.

        A server from before migration 016 refuses the whole record (422) because it does not know
        the newer fields. Sending the fields it knows keeps the rest of the record.

        Returns:
            The record that was sent: ``record``, or its older fields only.

        Raises:
            ApiUnavailableError, ApiRejectedError: As :meth:`ApiPort.put_environment`.
        """
        try:
            self._api.put_environment(session_id, record)
        except ApiRejectedError as error:
            legacy = record.restricted_to(LEGACY_FIELD_NAMES)
            if error.status != 422 or legacy == record or not legacy.to_payload():
                raise
            logger.info("the API does not know the newer environment fields: sending the others")
            self._api.put_environment(session_id, legacy)
            return legacy
        return record

    # ------------------------------------------------------------ lifecycle events

    def queue_lifecycle(self, session_id: str, event: LifecycleEvent) -> None:
        """Keep a lifecycle event in the state folder until :meth:`send_lifecycle` delivers it.

        Writing it first means a hook that is cut short, or an API that is down, loses nothing.
        At most :data:`MAX_PENDING_LIFECYCLE` events wait per session; the oldest go first.
        """
        name = f"{LIFECYCLE_PREFIX}{event.kind}-{event.run_key}"
        self._state.set_marker(session_id, name, json.dumps(event.to_payload()))
        waiting = self._pending_lifecycle(session_id)
        for old_name, _ in waiting[: max(0, len(waiting) - MAX_PENDING_LIFECYCLE)]:
            self._state.remove_marker(session_id, old_name)

    def add_end_commit(self, session_id: str, commit: str | None) -> bool:
        """Add the Git HEAD to the newest waiting session end that has none.

        The SessionEnd hook has almost no time, so it queues the end without reading Git; the
        detached flush it starts reads HEAD and adds it here before sending the end.

        Known gap (rare, accepted): if a catch-up started by another chat sends the waiting end
        before the flush got here, the end is recorded without its commit. The flush then finds no
        end to complete, and that session's ``end_commit`` stays empty.

        Returns:
            True if a waiting end was given the commit.
        """
        if not commit:
            return False
        ends = [
            (name, event)
            for name, event in self._pending_lifecycle(session_id)
            if event.kind == END and event.commit is None
        ]
        if not ends:
            return False
        name, event = ends[-1]
        self._state.set_marker(
            session_id, name, json.dumps(replace(event, commit=commit).to_payload())
        )
        return True

    def send_lifecycle(self, session_id: str) -> str | None:
        """Send the session's waiting lifecycle events, oldest first.

        Returns:
            ``None`` if nothing was waiting, otherwise a short note for the log (how many were sent,
            or why they wait). :data:`API_UNAVAILABLE` if the API cannot be reached,
            :data:`TOKEN_REFUSED` if it refuses the token.
        """
        waiting = self._pending_lifecycle(session_id)
        if not waiting:
            return None
        if self._is_paused(session_id, "lifecycle"):
            return "lifecycle events wait (the API does not record them yet)"
        sent = 0
        for name, event in waiting:
            try:
                self._api.post_lifecycle(session_id, event)
            except ApiUnavailableError as error:
                logger.warning("lifecycle event not sent: %s", error)
                return API_UNAVAILABLE
            except ApiRejectedError as error:
                if error.status in NOT_SUPPORTED_STATUSES:
                    self._pause(session_id, "lifecycle", error.status)
                    return "lifecycle events wait (the API does not record them yet)"
                if error.status in ACCESS_DENIED_STATUSES:
                    logger.error("lifecycle event refused (%d): check the token", error.status)
                    return TOKEN_REFUSED
                logger.error("lifecycle event refused and dropped: %s", error)
            self._state.remove_marker(session_id, name)
            sent += 1
        return f"{sent} lifecycle event(s) sent"

    def _pending_lifecycle(self, session_id: str) -> list[tuple[str, LifecycleEvent]]:
        """Return the waiting events with their marker names, oldest first (start before end).

        A marker that cannot be read back is removed (it could never be sent).
        """
        waiting: list[tuple[str, LifecycleEvent]] = []
        for name in self._state.marker_names(session_id, LIFECYCLE_PREFIX):
            try:
                event = parse_lifecycle(json.loads(self._state.get_marker(session_id, name) or ""))
            except (ValueError, LifecycleEventError):
                logger.warning("unreadable lifecycle event removed")
                self._state.remove_marker(session_id, name)
                continue
            waiting.append((name, event))
        return sorted(waiting, key=lambda item: (item[1].at, item[1].kind != START))

    # ------------------------------------------------------------ endpoints an older API lacks

    def _is_paused(self, session_id: str, feature: str) -> bool:
        """True if the API said less than an hour ago that it does not have this endpoint."""
        noted = self._state.get_marker(session_id, f"unsupported-{feature}")
        try:
            noted_at = float(noted) if noted is not None else None
        except ValueError:
            return False
        return noted_at is not None and self._wall_clock() - noted_at < UNSUPPORTED_RETRY_SECONDS

    def _pause(self, session_id: str, feature: str, status: int) -> None:
        """Note that the API does not have an endpoint yet (logged once per hour and session)."""
        logger.info("the API does not offer %s yet (%d); asking again in an hour", feature, status)
        self._state.set_marker(session_id, f"unsupported-{feature}", str(self._wall_clock()))

    # ------------------------------------------------------------ tool-result files

    def _sync_tool_results(self, session_id: str, folder: Path, deadline: float) -> StreamResult:
        """Upload the session's new or changed tool-result files, redacted.

        A file is remembered by its size and modification time once it is uploaded (or given up
        for good), so an unchanged file is never read again.
        """
        result = StreamResult(TOOL_RESULTS_FOLDER)
        if self._is_paused(session_id, "tool-results"):
            return result
        try:
            paths = sorted(path for path in folder.iterdir() if path.is_file())
        except OSError as error:
            result.error = f"cannot list tool results: {type(error).__name__}"
            return result
        for path in paths:
            if self._clock() >= deadline:
                return result
            step = self._sync_tool_result(session_id, path, result)
            if step is _Step.STOP:
                return result
        result.caught_up = True
        return result

    def _sync_tool_result(self, session_id: str, path: Path, result: StreamResult) -> _Step:
        """Upload one tool-result file unless it was already done.

        A file with a name that cannot be stored is remembered like an uploaded one, so it is
        logged once and not again at every sync.
        """
        try:
            stat = path.stat()
        except OSError:
            return _Step.LATER  # it disappeared, or cannot be read now
        marker = TOOL_RESULT_PREFIX + hashlib.sha256(path.name.encode()).hexdigest()[:16]
        signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        if self._state.get_marker(session_id, marker) == signature:
            return _Step.DONE
        try:
            key = tool_result_key(session_id, path.name)
        except ValueError:
            logger.warning("a tool-result file with an unusable name was skipped")
            step = _Step.DONE
        else:
            step = self._upload_tool_result(session_id, path, key, stat.st_size, result)
        if step is _Step.DONE:
            self._state.set_marker(session_id, marker, signature)
        return step

    def _upload_tool_result(
        self, session_id: str, path: Path, key: str, size: int, result: StreamResult
    ) -> _Step:
        """Redact and upload one file, and report where redaction rules fired."""
        if size > MAX_TOOL_RESULT_BYTES:
            logger.warning("a tool-result file of %d bytes is too large to upload: skipped", size)
            return _Step.DONE
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as error:
            result.error = f"cannot read a tool result: {type(error).__name__}"
            return _Step.LATER
        try:
            cleaned, fired = _redact_file_text(text)
        except Exception:  # noqa: BLE001  (fail closed: never upload what could not be cleaned)
            logger.error("a tool-result file could not be redacted: withheld")
            return _Step.DONE
        body = cleaned.encode("utf-8")
        if len(body) > MAX_TOOL_RESULT_BYTES:
            logger.warning("a tool-result file is too large to upload after redaction: skipped")
            return _Step.DONE
        try:
            reply = self._api.put_tool_result(session_id, path.name, body)
            if reply.stored:
                result.files += 1
            if fired:
                hits = [Hit(rule_id, 0, "") for rule_id in fired]
                stored_at = _stored_key(reply, key)
                self._api.post_redactions(session_id, records_from_hits(hits, stored_at))
        except ApiUnavailableError as error:
            _stop_unavailable(error, result)
            return _Step.STOP
        except ApiRejectedError as error:
            return self._after_tool_result_refusal(session_id, error, result)
        return _Step.DONE

    def _after_tool_result_refusal(
        self, session_id: str, error: ApiRejectedError, result: StreamResult
    ) -> _Step:
        """Decide what a refused tool-result upload means (never counted as a chunk refusal)."""
        if error.status in NOT_SUPPORTED_STATUSES:
            self._pause(session_id, "tool-results", error.status)
            return _Step.STOP
        if error.status in ACCESS_DENIED_STATUSES:
            _stop_access_denied(error, result)
            return _Step.STOP
        logger.error("a tool-result file was refused and is skipped: %s", error)
        return _Step.DONE

    # ------------------------------------------------------------ one stream

    def _sync_lines(
        self, session_id: str, stream: str, path: Path, deadline: float
    ) -> StreamResult:
        """Sync a JSONL stream from its saved position, one read window after another."""
        result = StreamResult(stream)
        if not _is_allowed_stream(stream):
            result.error = "stream name is not allowed"
            return result
        with self._state.lock(session_id, stream, self._lock_wait) as acquired:
            if not acquired:
                result.error = "another sync of this stream is running"
                return result
            offset = self._state.get_offset(session_id, stream)
            while True:
                try:
                    read = read_complete_lines(path, offset)
                except OSError as error:
                    result.error = f"cannot read transcript: {type(error).__name__}"
                    return result
                for chunk in split_into_chunks(read.lines, self._chunk_bytes):
                    if self._clock() >= deadline:
                        return result  # out of time: the next sync goes on from the position
                    step = self._upload_chunk(session_id, stream, offset, chunk, result)
                    if step is None:
                        return result  # stopped; result.error says why
                    offset = step
                if read.reached_end:
                    result.caught_up = True
                    return result
                # The read window was full: read on from the new position.

    def _sync_meta(self, session_id: str, stream: str, path: Path, deadline: float) -> StreamResult:
        """Sync a sub-agent's ``.meta.json`` file: one small JSON object, uploaded once."""
        result = StreamResult(stream)
        if not _is_allowed_stream(stream):
            result.error = "stream name is not allowed"
            return result
        with self._state.lock(session_id, stream, self._lock_wait) as acquired:
            if not acquired:
                result.error = "another sync of this stream is running"
                return result
            try:
                size = path.stat().st_size
                if self._state.get_offset(session_id, stream) >= size:
                    result.caught_up = True  # already uploaded
                    return result
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except (OSError, ValueError) as error:
                result.error = f"cannot read file: {type(error).__name__}"
                return result
            if self._clock() >= deadline:
                return result
            text = _with_workflow_run(text, path)
            line = SourceLine(" ".join(text.splitlines()), size)  # to a single line
            end = self._upload_chunk(session_id, stream, 0, [line], result)
            result.caught_up = end is not None
        return result

    # ------------------------------------------------------------ one chunk

    def _upload_chunk(
        self,
        session_id: str,
        stream: str,
        offset: int,
        lines: list[SourceLine],
        result: StreamResult,
    ) -> int | None:
        """Clean, upload and record one chunk.

        Returns:
            The new position, or ``None`` if the upload stopped (``result.error`` says why).
            The position is saved only after every step succeeded.
        """
        key = transcript_chunk_key(session_id, stream, offset)
        prepared = prepare_chunk(key, lines)
        try:
            if prepared.body:
                for digest, body in prepared.attachments.items():
                    if self._api.put_attachment(digest, body):
                        result.attachments += 1
                self._api.put_chunk(session_id, stream, offset, prepared.body)
                if prepared.records:
                    self._api.post_redactions(session_id, prepared.records)
        except ApiUnavailableError as error:
            _stop_unavailable(error, result)
            return None
        except ApiRejectedError as error:
            return self._after_refusal(session_id, stream, offset, lines, error, result)
        if prepared.body:
            result.chunks += 1
        return self._advance(session_id, stream, offset, prepared)

    def _after_refusal(
        self,
        session_id: str,
        stream: str,
        offset: int,
        lines: list[SourceLine],
        error: ApiRejectedError,
        result: StreamResult,
    ) -> int | None:
        """Decide what to do after the API refused part of a chunk's upload.

        A refused token stops the run. Any other refusal is counted; once the same chunk was
        refused :data:`MAX_REFUSALS` times in a row, a placeholder is uploaded in its place.

        Returns:
            The new position, or ``None`` if the stream stops here.
        """
        if error.status in ACCESS_DENIED_STATUSES:
            _stop_access_denied(error, result)
            return None
        refusals = self._count_refusal(session_id, stream, offset)
        if refusals < MAX_REFUSALS:
            logger.error(
                "upload of %s at %d refused (%d of %d before it is skipped): %s",
                stream,
                offset,
                refusals,
                MAX_REFUSALS,
                error,
            )
            result.error = str(error)
            return None
        logger.error(
            "chunk of %s at %d refused %d times in a row (%d): a placeholder is uploaded instead",
            stream,
            offset,
            refusals,
            error.status,
        )
        return self._upload_placeholder(session_id, stream, offset, lines, error.status, result)

    def _count_refusal(self, session_id: str, stream: str, offset: int) -> int:
        """Record one more refusal of the chunk at ``offset``; return how many in a row.

        The count lives in a state marker, ``"<offset> <count>"``. A marker for another offset
        belongs to an earlier chunk that has since gone through, so counting starts again.
        """
        name = f"refused-{stream}"
        previous = (self._state.get_marker(session_id, name) or "").split()
        count = 1
        if len(previous) == 2 and previous[0] == str(offset) and previous[1].isdigit():
            count = int(previous[1]) + 1
        self._state.set_marker(session_id, name, f"{offset} {count}")
        return count

    def _upload_placeholder(
        self,
        session_id: str,
        stream: str,
        offset: int,
        lines: list[SourceLine],
        status: int,
        result: StreamResult,
    ) -> int | None:
        """Upload a placeholder in place of a chunk the API keeps refusing, and move past it.

        If the placeholder is refused too (other than for the token), the position still moves:
        the chunk's content is already given up, and the stream must not stay blocked.

        Returns:
            The new position, or ``None`` if the API is unavailable or refuses the token.
        """
        placeholder = refused_chunk(lines, status)
        try:
            self._api.put_chunk(session_id, stream, offset, placeholder.body)
        except ApiUnavailableError as error:
            _stop_unavailable(error, result)
            return None
        except ApiRejectedError as error:
            if error.status in ACCESS_DENIED_STATUSES:
                _stop_access_denied(error, result)
                return None
            logger.error("placeholder refused too (%d): moving past the chunk", error.status)
        result.skipped += 1
        return self._advance(session_id, stream, offset, placeholder)

    def _advance(self, session_id: str, stream: str, offset: int, prepared: PreparedChunk) -> int:
        """Save the position after a chunk that is done with; return the new position."""
        new_offset = offset + prepared.source_bytes
        self._state.set_offset(session_id, stream, new_offset)
        return new_offset


# ------------------------------------------------------------ helpers


def _stop_unavailable(error: ApiUnavailableError, result: StreamResult) -> None:
    """Record that the API could not be reached; the next sync tries again."""
    logger.warning("upload paused: %s", error)
    result.error = "the API is unavailable; will retry on the next sync"
    result.api_unavailable = True


def _stop_access_denied(error: ApiRejectedError, result: StreamResult) -> None:
    """Record that the API refused the token; nothing more is tried in this run.

    The message names the setting to check, never the token itself.
    """
    logger.error(
        "upload refused (%d): the API does not accept the token (check CCP_INGEST_TOKEN); "
        "no other stream or session is tried in this run",
        error.status,
    )
    result.error = f"the API refused the token ({error.status}); check CCP_INGEST_TOKEN"
    result.access_denied = True


def _stored_key(reply: ToolResultReply, key: str) -> str:
    """Return the key the server stored a tool-result file under, or ``key`` if it did not say.

    Only the file's own key or a version of it (``<key>@<hash>``) is accepted, so that a wrong
    answer cannot point a redaction report elsewhere.
    """
    if reply.key is not None and (reply.key == key or reply.key.startswith(key + "@")):
        return reply.key
    return key


def _digest(payload: dict[str, Any]) -> str:
    """Return a hash of an environment payload (to tell whether it was sent already)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _redact_file_text(text: str) -> tuple[str, list[str]]:
    r"""Redact the text of a tool-result file; return it with the rule id of every redaction.

    A file that holds a JSON object or list is redacted value by value first, as transcript lines
    are: inside JSON a secret is escaped (a line break before it is written ``\n``), so some rules
    would miss it in the raw text. Only if that finds something is the file written out again (as
    JSON, so its layout changes); otherwise it stays as it was. The raw-text rules run in any case.
    """
    fired: list[str] = []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict | list):
        found: list[tuple[str, str]] = []
        cleaned = redact_value(parsed, "", DEFAULT_RULES, found)
        if found:
            text = json.dumps(cleaned, ensure_ascii=False, indent=2)
            fired.extend(rule_id for rule_id, _ in found)
    text, more = redact_string(text)
    return text, fired + more


def _with_workflow_run(text: str, path: Path) -> str:
    """Add the Workflow run id to a sub-agent description stored in a Workflow run's folder.

    The file ``subagents/workflows/<run id>/agent-<id>.meta.json`` does not name its run; the
    folder does (observed on Claude Code 2.1.325). Other descriptions are returned unchanged, and
    so is one that is not a JSON object.
    """
    run_folder = path.parent
    if run_folder.parent.name != "workflows" or run_folder.parent.parent.name != "subagents":
        return text
    try:
        description = json.loads(text)
        run_id = check_id("workflow run id", run_folder.name)
    except ValueError:
        return text
    if not isinstance(description, dict):
        return text
    description[WORKFLOW_RUN_KEY] = run_id
    return json.dumps(description, ensure_ascii=False, separators=(",", ":"))


def _is_allowed_stream(stream: str) -> bool:
    """Return True if ``stream`` is a safe stream name (see :func:`ccp.storage.keys.check_id`)."""
    try:
        check_id("stream", stream)
    except ValueError:
        return False
    return True


def _agent_files(folder: Path, suffix: str) -> dict[str, Path]:
    """Find a session's sub-agent files ending in ``suffix``, by stream name.

    Sub-agents started directly are stored in ``subagents/``; those started by the Workflow tool
    in ``subagents/workflows/<run id>/``. Both are searched (other files there, such as a
    workflow's ``journal.jsonl``, are not sub-agents and are left alone).

    Args:
        folder: The session's ``subagents`` folder.
        suffix: ``".jsonl"`` for transcripts (stream ``agent-<id>``) or ``".meta.json"`` for
            meta files (stream ``agent-<id>.meta``).

    Returns:
        The files by stream name, in a stable order. Agent ids are unique, so a name found twice
        is unexpected: only the first file is used (two files cannot share one saved position)
        and the other is logged.
    """
    found: dict[str, Path] = {}
    for path in sorted(folder.rglob(f"agent-*{suffix}")):
        stream = path.name.removesuffix(".jsonl").removesuffix(".json")  # agent-<id>[.meta]
        if stream in found:
            logger.warning("sub-agent stream %s found twice; only the first file is used", stream)
            continue
        found[stream] = path
    return found
