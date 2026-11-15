"""The indexer worker as a background thread, and the thread that runs after-index callbacks.

The worker loop itself is :func:`ccp.index.worker.run`; this module only starts it in a thread,
stops it cleanly, and calls the plug-ins' callbacks after each finished job.

Callbacks run in their **own** thread, fed by a small queue: a callback may take minutes (the
decisions call a language model), and indexing must not wait for it. A session that is already
waiting in the queue is not queued twice.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from typing import Any, cast

from ccp.index.repository import IndexJob
from ccp.index.worker import ChunkStore, JobRepository, run

logger = logging.getLogger("ccp.server")

STOP_TIMEOUT_SECONDS = 30.0


class _Stop(Exception):  # noqa: N818  (a signal, not an error)
    """Raised inside the worker loop to leave it when the server shuts down."""


class CallbackRunner:
    """Runs the after-index callbacks for queued session ids, one session at a time."""

    def __init__(self, callbacks: Sequence[Callable[[str], None]]) -> None:
        """Create the runner. It does nothing until :meth:`start`."""
        self._callbacks = list(callbacks)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._waiting: set[str] = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="ccp-after-index", daemon=True)

    def start(self) -> None:
        """Start the thread (nothing to do if there are no callbacks)."""
        if self._callbacks:
            self._thread.start()

    def submit(self, session_id: str) -> None:
        """Queue a session, unless it is already waiting."""
        if not self._callbacks:
            return
        with self._lock:
            if session_id in self._waiting:
                return
            self._waiting.add(session_id)
        self._queue.put(session_id)

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Skip what is still queued and wait for the running callback (up to ``timeout``)."""
        self._stopping.set()
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            session_id = self._queue.get()
            if session_id is None or self._stopping.is_set():
                return
            with self._lock:
                self._waiting.discard(session_id)
            for callback in self._callbacks:
                try:
                    callback(session_id)
                except Exception as error:  # noqa: BLE001  (one callback must not stop the rest)
                    logger.exception(
                        "after-index callback failed for session %s (%s)",
                        session_id,
                        type(error).__name__,
                    )


class _NotifyingRepository:
    """The indexer's repository, plus a notice to ``on_done`` when a job has been finished.

    Every other call is passed on unchanged. ``claim_job`` also raises :class:`_Stop` once the
    server is shutting down, which ends the worker loop between two jobs.
    """

    def __init__(
        self, inner: JobRepository, stop: threading.Event, on_done: Callable[[str], None]
    ) -> None:
        self._inner = inner
        self._stop = stop
        self._on_done = on_done
        self._sessions: dict[int, str] = {}

    def claim_job(self) -> IndexJob | None:
        if self._stop.is_set():
            raise _Stop
        job = self._inner.claim_job()
        if job is not None:
            self._sessions[job.job_id] = job.session_id
        return job

    def finish_job(self, job_id: int) -> None:
        self._inner.finish_job(job_id)
        session_id = self._sessions.pop(job_id, None)
        if session_id is not None:
            self._on_done(session_id)

    def fail_job(self, job_id: int) -> bool:
        self._sessions.pop(job_id, None)
        return self._inner.fail_job(job_id)

    def __getattr__(self, name: str) -> Any:
        """Pass every other repository call on unchanged.

        Delegating by name (instead of listing each method) means a method the indexer's
        repository gains later keeps working here without a change to this class.
        """
        return getattr(self._inner, name)


class IndexerThread:
    """The indexer loop in a background thread that can be stopped."""

    def __init__(
        self,
        store: ChunkStore,
        repository: JobRepository,
        *,
        poll_seconds: float,
        callbacks: Sequence[Callable[[str], None]] = (),
    ) -> None:
        """Create the thread (not started).

        Args:
            store: Where the transcript chunks are.
            repository: The index database.
            poll_seconds: How long to wait when the queue is empty.
            callbacks: Called with a session id after each finished job.
        """
        self._store = store
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._callbacks = CallbackRunner(callbacks)
        self._repository = cast(
            JobRepository, _NotifyingRepository(repository, self._stop, self._callbacks.submit)
        )
        self._thread = threading.Thread(target=self._run, name="ccp-indexer", daemon=True)

    def start(self) -> None:
        """Start indexing (and the callbacks' thread)."""
        self._callbacks.start()
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Stop after the job in progress and wait for the threads (up to ``timeout`` each)."""
        self._stop.set()
        self._thread.join(timeout)
        self._callbacks.stop(timeout)
        if self._thread.is_alive():
            logger.warning("the indexer did not stop within %s s", timeout)

    @property
    def running(self) -> bool:
        """True while the indexer thread is alive."""
        return self._thread.is_alive()

    def _sleep(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise _Stop

    def _run(self) -> None:
        """Run the worker loop; start it again after an unexpected failure."""
        while not self._stop.is_set():
            try:
                run(
                    self._store,
                    self._repository,
                    poll_seconds=self._poll_seconds,
                    sleep=self._sleep,
                )
            except _Stop:
                return
            except Exception as error:  # noqa: BLE001  (the indexer must outlive any failure)
                logger.exception("the indexer failed (%s); starting it again", type(error).__name__)
                if self._stop.wait(self._poll_seconds):
                    return
