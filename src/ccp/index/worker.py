"""Indexing a session, and the loop that works through the job queue.

The ingestion API queues one ``index_session`` job per session whenever chunks arrive (and once
more when the session is closed). A job means "bring the index of this session up to date":

1. list the session's stored transcript chunks (the main transcript and any sub-agent streams);
2. skip chunks that were indexed before: a chunk never changes, because its key contains its
   position in the source file;
3. for each new chunk, read every line and store one event per line;
4. recompute what is derived from the events: abandoned branches, compactions, session facts, model
   usage and cost, sub-agent runs.

A sub-agent's small description file (stream ``agent-<id>.meta``) is not a transcript: it is read
into the ``agent_run`` table instead of becoming events.

Doing it this way makes indexing **idempotent**: running a job again, or two jobs for the same
session, never creates a duplicate. A chunk is stored in one transaction, so a crash leaves a
chunk either fully indexed or not at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ccp.index.agents import (
    AgentMeta,
    AgentMetaError,
    agent_id_of,
    is_meta_stream,
    parse_agent_meta,
)
from ccp.index.parser import ParsedEntry, ParseError, parse_line
from ccp.index.repository import IndexJob
from ccp.storage.keys import session_prefix, stream_of

logger = logging.getLogger("ccp.index")


class ChunkStore(Protocol):
    """The part of the object store the indexer reads."""

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under ``prefix``, in alphabetical order."""

    def get(self, key: str) -> bytes:
        """The content of an object."""


class JobRepository(Protocol):
    """The part of the database the indexer uses."""

    def claim_job(self) -> IndexJob | None:
        """Take the oldest queued job, or None."""

    def finish_job(self, job_id: int) -> None:
        """Mark a job done."""

    def fail_job(self, job_id: int) -> bool:
        """Record a failure; True if the job will be tried again."""

    def indexed_keys(self, session_id: str) -> set[str]:
        """Chunks of the session that are already indexed."""

    def save_chunk(
        self, session_id: str, object_key: str, entries: Sequence[tuple[int, ParsedEntry]]
    ) -> None:
        """Store one chunk's events."""

    def save_agent_meta(self, session_id: str, agent_id: str, meta: AgentMeta) -> None:
        """Store a sub-agent's description."""

    def refresh_session(self, session_id: str) -> None:
        """Recompute what is derived from the session's events (tree, compactions, facts)."""


@dataclass(frozen=True)
class IndexResult:
    """What one indexing run did."""

    chunks_indexed: int
    events_stored: int
    lines_skipped: int  # lines that were not JSON objects (the server should have refused them)


def index_session(store: ChunkStore, repository: JobRepository, session_id: str) -> IndexResult:
    """Index every chunk of a session that is not indexed yet.

    Args:
        store: Where the transcript chunks are.
        repository: Where the events go.
        session_id: The session to bring up to date.

    Returns:
        How many chunks and events were added. All zeros means the index was already current.
    """
    done = repository.indexed_keys(session_id)
    chunks = events = skipped = 0
    for key in store.list_keys(session_prefix(session_id)):
        if not key.endswith(".jsonl"):
            continue
        stream = stream_of(key)
        if is_meta_stream(stream):
            # A description, not a transcript: it has no events. It is tiny and stored by an
            # upsert, so it is read again every time instead of being tracked.
            skipped += not _save_meta(store, repository, session_id, stream, key)
            continue
        if key in done:
            continue
        entries, bad_lines = _read_chunk(store.get(key))
        repository.save_chunk(session_id, key, entries)
        chunks += 1
        events += len(entries)
        skipped += bad_lines
    # Always, even when no chunk was new: it is cheap, and it also brings sessions that were
    # indexed by an earlier version of the indexer up to date.
    repository.refresh_session(session_id)
    return IndexResult(chunks, events, skipped)


def _save_meta(
    store: ChunkStore, repository: JobRepository, session_id: str, stream: str, key: str
) -> bool:
    """Store one sub-agent description. Returns False if it could not be read (it is skipped)."""
    try:
        meta = parse_agent_meta(store.get(key).decode("utf-8").strip())
    except (AgentMetaError, UnicodeDecodeError):
        return False
    repository.save_agent_meta(session_id, agent_id_of(stream), meta)
    return True


def _read_chunk(data: bytes) -> tuple[list[tuple[int, ParsedEntry]], int]:
    """Parse a chunk into ``(line number, entry)`` pairs, counting lines that cannot be read."""
    entries: list[tuple[int, ParsedEntry]] = []
    bad_lines = 0
    # Split on "\n" only (see ccp.ingest.chunks): the last piece is empty after the final newline.
    for number, line in enumerate(data.decode("utf-8").split("\n")[:-1], start=1):
        try:
            entries.append((number, parse_line(line)))
        except ParseError:
            bad_lines += 1
    return entries, bad_lines


def process_next_job(store: ChunkStore, repository: JobRepository) -> bool:
    """Take one job from the queue and run it.

    A failure is recorded on the job (it is tried again up to a limit) and logged; it does not
    stop the caller.

    Returns:
        True if a job was taken (whether it succeeded or not), False if the queue was empty.
    """
    job = repository.claim_job()
    if job is None:
        return False
    try:
        result = index_session(store, repository, job.session_id)
    except Exception:  # noqa: BLE001  (a bad session must not stop the worker)
        will_retry = repository.fail_job(job.job_id)
        logger.exception(
            "indexing session %s failed (attempt %d); %s",
            job.session_id,
            job.attempts,
            "will retry" if will_retry else "giving up",
        )
        return True
    repository.finish_job(job.job_id)
    logger.info(
        "indexed session %s: %d chunk(s), %d event(s)%s",
        job.session_id,
        result.chunks_indexed,
        result.events_stored,
        f", {result.lines_skipped} unreadable line(s)" if result.lines_skipped else "",
    )
    return True


def run(
    store: ChunkStore,
    repository: JobRepository,
    *,
    poll_seconds: float,
    once: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Work through the queue.

    Args:
        store: Where the transcript chunks are.
        repository: The database.
        poll_seconds: How long to wait when the queue is empty.
        once: Stop when the queue is empty instead of waiting for more jobs.
        sleep: How to wait (replaced in tests).

    Returns:
        The number of jobs handled (only reached when ``once`` is true).
    """
    handled = 0
    while True:
        if process_next_job(store, repository):
            handled += 1
        elif once:
            return handled
        else:
            sleep(poll_seconds)
