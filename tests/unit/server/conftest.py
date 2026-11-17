"""Fakes for the server tests: no database, no MinIO, no network."""

import threading
from collections.abc import Sequence

from ccp.index.agents import AgentMeta
from ccp.index.parser import ParsedEntry
from ccp.index.repository import IndexJob, IndexRepositoryError
from ccp.settings import Settings, load_settings
from ccp.storage import StorageError


class FakeStore:
    """An empty bucket (the indexer finds no chunks), or a dictionary of objects."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.down = False

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        if self.down:
            raise StorageError("down")
        self.objects[key] = data

    def exists(self, key: str) -> bool:
        if self.down:
            raise StorageError("down")
        return key in self.objects


class FakeJobs:
    """A job queue shared by the fake API repository and the fake index repository."""

    def __init__(self) -> None:
        self.queued: list[str] = []
        self.done: list[str] = []
        self.failed: list[str] = []
        self.lock = threading.Lock()
        self.fail_claims = 0  # the next N claims raise a database error
        self.crash_claims = 0  # the next N claims raise an unexpected error
        self.refreshed: list[str] = []
        self.saved_chunks: list[str] = []
        self.test_outputs: list[str] = []
        self._next_id = 0
        self._running: dict[int, str] = {}
        self.finished = threading.Event()

    def queue(self, session_id: str) -> None:
        with self.lock:
            self.queued.append(session_id)

    # -- the part of the index repository the worker uses
    def claim_job(self) -> IndexJob | None:
        with self.lock:
            if self.fail_claims:
                self.fail_claims -= 1
                raise IndexRepositoryError("database down")
            if self.crash_claims:
                self.crash_claims -= 1
                raise RuntimeError("unexpected")
            if not self.queued:
                return None
            self._next_id += 1
            session_id = self.queued.pop(0)
            self._running[self._next_id] = session_id
            return IndexJob(self._next_id, session_id, 1)

    def finish_job(self, job_id: int) -> None:
        self.done.append(self._running.pop(job_id))
        self.finished.set()

    def fail_job(self, job_id: int) -> bool:
        self.failed.append(self._running.pop(job_id))
        return False

    def indexed_keys(self, session_id: str) -> set[str]:
        return set()

    def save_chunk(
        self, session_id: str, object_key: str, entries: Sequence[tuple[int, ParsedEntry]]
    ) -> None:
        self.saved_chunks.append(object_key)

    def save_agent_meta(self, session_id: str, agent_id: str, meta: AgentMeta) -> None:
        raise AssertionError("the fake bucket has no sub-agents")

    def refresh_session(self, session_id: str) -> None:
        self.refreshed.append(session_id)

    def refresh_test_outputs(self, session_id: str, store: object) -> None:
        self.test_outputs.append(session_id)


class FakeSessionRepository:
    """The API's repository: a stored chunk queues an index job, like the real one."""

    def __init__(self, jobs: FakeJobs) -> None:
        self._jobs = jobs

    def record_chunk(self, session_id: str) -> None:
        self._jobs.queue(session_id)

    def ping(self) -> None:
        return None


def make_settings() -> Settings:
    """Settings for tests, built from a dictionary (no ``.env`` file is read)."""
    return load_settings(
        {
            "POSTGRES_DB": "ccp_unit",
            "POSTGRES_USER": "ccp",
            "POSTGRES_PASSWORD": "p" * 12,
            "MINIO_ACCESS_KEY": "minio-access",
            "MINIO_SECRET_KEY": "minio-secret-key",
            "CCP_INGEST_TOKEN": "t" * 24,
        },
        env_file=None,
    )
