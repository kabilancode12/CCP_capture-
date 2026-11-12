"""The capture client: uploads new transcript lines to the ingestion API.

How a sync works, for one stream (a session transcript or one sub-agent):

1. Take the stream's lock, so two hooks cannot upload it at the same time.
2. Read the complete lines after the saved position and group them into chunks.
3. For each chunk, in order: clean it (redaction, attachment de-duplication), upload its
   attachments, upload the chunk, report its redactions, and **only then** save the new position.

Nothing is lost when the API is down or the machine is offline: the position does not move, the
transcript file keeps the data, and the next sync repeats the same steps. Every step is safe to
repeat (the server answers "already stored"), so a failure halfway is harmless.
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
from ccp.capture.pipeline import prepare_chunk
from ccp.capture.reader import SourceLine, TranscriptError, read_complete_lines, split_into_chunks
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.storage.keys import MAIN_STREAM, check_id, transcript_chunk_key

logger = logging.getLogger("ccp.capture")

DEFAULT_CHUNK_BYTES = 1024 * 1024
DEFAULT_BUDGET_SECONDS = 20.0
DEFAULT_LOCK_WAIT_SECONDS = 10.0


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
    """

    stream: str
    chunks: int = 0
    attachments: int = 0
    caught_up: bool = False
    error: str | None = None
    api_unavailable: bool = False


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
            One result per stream that was tried. If the API is unavailable, the remaining
            streams are not tried (they would fail the same way, and slowly).
        """
        deadline = self._clock() + (self._budget if budget_seconds is None else budget_seconds)
        return self._sync_session_until(session_id, transcript_path, deadline)

    def catch_up(self, budget_seconds: float | None = None) -> dict[str, list[StreamResult]]:
        """Sync every session the client has seen whose transcript file still exists.

        Used at session start to pick up what earlier sessions left behind (a session that ended
        abruptly, an upload that failed). It shares one time limit across all sessions and stops
        at the first "API unavailable".

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
                if any(r.api_unavailable for r in results[session_id]):
                    break  # the API is down: the other sessions would fail the same way
        return results

    def _sync_session_until(
        self, session_id: str, transcript_path: Path, deadline: float
    ) -> list[StreamResult]:
        """Sync a session's streams until ``deadline`` (see :meth:`sync_session`)."""
        self._state.remember_transcript(session_id, transcript_path)
        results = [self._sync_lines(session_id, MAIN_STREAM, transcript_path, deadline)]

        agents_folder = transcript_path.with_suffix("") / "subagents"
        if agents_folder.is_dir() and not results[0].api_unavailable:
            for path in sorted(agents_folder.glob("agent-*.jsonl")):
                results.append(self._sync_lines(session_id, path.stem, path, deadline))
                if results[-1].api_unavailable:
                    return results
            for path in sorted(agents_folder.glob("agent-*.meta.json")):
                results.append(self._sync_meta(session_id, path, deadline))
                if results[-1].api_unavailable:
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
        """Sync a JSONL stream from its saved position."""
        result = StreamResult(stream)
        try:
            check_id("stream", stream)
        except ValueError:
            result.error = "stream name is not allowed"
            return result
        with self._state.lock(session_id, stream, self._lock_wait) as acquired:
            if not acquired:
                result.error = "another sync of this stream is running"
                return result
            offset = self._state.get_offset(session_id, stream)
            try:
                read = read_complete_lines(path, offset)
            except (TranscriptError, FileNotFoundError) as error:
                result.error = f"cannot read transcript: {type(error).__name__}"
                return result
            for chunk in split_into_chunks(read.lines, self._chunk_bytes):
                if self._clock() >= deadline:
                    return result  # out of time: the next sync continues from the saved position
                step = self._upload_chunk(session_id, stream, offset, chunk, result)
                if step is None:
                    return result  # stopped; result.error says why
                offset = step
            result.caught_up = read.reached_end
        return result

    def _sync_meta(self, session_id: str, path: Path, deadline: float) -> StreamResult:
        """Sync a sub-agent's ``.meta.json`` file: one small JSON object, uploaded once."""
        stream = path.name.removesuffix(".json")  # agent-<id>.meta
        result = StreamResult(stream)
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
            logger.warning("upload paused: %s", error)
            result.error = "the API is unavailable; will retry on the next sync"
            result.api_unavailable = True
            return None
        except ApiRejectedError as error:
            logger.error("upload refused: %s", error)
            result.error = str(error)
            return None
        new_offset = offset + prepared.source_bytes
        self._state.set_offset(session_id, stream, new_offset)
        if prepared.body:
            result.chunks += 1
        return new_offset
