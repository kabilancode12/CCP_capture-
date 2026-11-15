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
(agent ids are unique within a session).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ccp.capture.api import ApiPort, ApiRejectedError, ApiUnavailableError
from ccp.capture.pipeline import PreparedChunk, prepare_chunk, refused_chunk
from ccp.capture.reader import SourceLine, read_complete_lines, split_into_chunks
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.storage.keys import MAIN_STREAM, check_id, transcript_chunk_key

logger = logging.getLogger("ccp.capture")

DEFAULT_CHUNK_BYTES = 1024 * 1024
DEFAULT_BUDGET_SECONDS = 20.0
DEFAULT_LOCK_WAIT_SECONDS = 10.0

# A chunk refused this many times in a row is replaced by a placeholder.
MAX_REFUSALS = 3

# Refusals that mean the token is not accepted: every request would be refused the same way.
ACCESS_DENIED_STATUSES = frozenset({401, 403})


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
    """

    stream: str
    chunks: int = 0
    attachments: int = 0
    caught_up: bool = False
    error: str | None = None
    api_unavailable: bool = False
    access_denied: bool = False
    skipped: int = 0

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
            clock: A function returning seconds (replaced in tests).
        """
        self._api = api
        self._state = state
        self._chunk_bytes = chunk_bytes
        self._budget = budget_seconds
        self._lock_wait = lock_wait_seconds
        self._clock = clock

    # ------------------------------------------------------------ public

    def sync_session(
        self,
        session_id: str,
        transcript_path: Path,
        budget_seconds: float | None = None,
    ) -> list[StreamResult]:
        """Upload everything new for a session: its transcript and its sub-agents' files.

        Args:
            session_id: Claude Code's session id.
            transcript_path: The session transcript (``<session>.jsonl``). Sub-agent files are
                found next to it in ``<session>/subagents/``.
            budget_seconds: Time limit for this call instead of the client's default (a hook that
                holds up Claude Code, such as UserPromptSubmit, uses a short one).

        Returns:
            One result per stream that was tried. If the API is unavailable or refuses the token,
            the remaining streams are not tried (they would fail the same way, and slowly).
        """
        deadline = self._clock() + (self._budget if budget_seconds is None else budget_seconds)
        return self._sync_session_until(session_id, transcript_path, deadline)

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
        for session_id, path in self._state.known_sessions():
            if self._clock() >= deadline:
                break
            if path.is_file():
                results[session_id] = self._sync_session_until(session_id, path, deadline)
                if any(r.stops_run for r in results[session_id]):
                    break  # the API is down or refuses us: the other sessions would fail too
        return results

    def _sync_session_until(
        self, session_id: str, transcript_path: Path, deadline: float
    ) -> list[StreamResult]:
        """Sync a session's streams until ``deadline`` (see :meth:`sync_session`)."""
        self._state.remember_transcript(session_id, transcript_path)
        results = [self._sync_lines(session_id, MAIN_STREAM, transcript_path, deadline)]

        agents_folder = transcript_path.with_suffix("") / "subagents"
        if agents_folder.is_dir() and not results[0].stops_run:
            for stream, path in _agent_files(agents_folder, ".jsonl").items():
                results.append(self._sync_lines(session_id, stream, path, deadline))
                if results[-1].stops_run:
                    return results
            for stream, path in _agent_files(agents_folder, ".meta.json").items():
                results.append(self._sync_meta(session_id, stream, path, deadline))
                if results[-1].stops_run:
                    return results
        return results

    def report_environment(self, session_id: str, record: EnvironmentRecord) -> str | None:
        """Send the session's environment record, unless the server already has this exact record.

        The record is sent only if it differs from what was last sent successfully (remembered as
        a hash), so calling this at every hook costs nothing once the record is complete.

        Args:
            session_id: Claude Code's session id.
            record: The known facts (see :mod:`ccp.capture.environment`).

        Returns:
            ``None`` on success (including "nothing to send"), otherwise a short reason that never
            contains the record's values.
        """
        payload = record.to_payload()
        if not payload:
            return None
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if self._state.get_marker(session_id, "environment") == digest:
            return None  # already sent
        try:
            self._api.put_environment(session_id, record)
        except ApiUnavailableError as error:
            logger.warning("environment not sent: %s", error)
            return "the API is unavailable; will retry on the next sync"
        except ApiRejectedError as error:
            logger.error("environment refused: %s", error)
            return str(error)
        self._state.set_marker(session_id, "environment", digest)
        return None

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
