"""Fakes and fixtures for the ingestion API unit tests (no database or MinIO needed)."""

from collections.abc import Sequence

import pytest
from starlette.testclient import TestClient

from ccp.environment import EnvironmentRecord
from ccp.ingest.app import IngestService
from ccp.ingest.repository import RepositoryError, RunKeyConflictError
from ccp.lifecycle import LifecycleEvent
from ccp.redaction.records import RedactionRecord
from ccp.settings import Secret
from ccp.storage import StorageError

TOKEN = "t" * 24  # test token
SESSION = "sess-1"
URL = f"/v1/sessions/{SESSION}/streams/main/chunks/0"
LINE = b'{"type":"user","uuid":"u1"}\n'
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeStore:
    """A dictionary standing in for the object store; can be told to fail."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise StorageError("down")

    def put(self, key: str, data: bytes, content_type: str = "") -> None:
        self._check()
        self.put_calls += 1
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        self._check()
        return self.objects[key]

    def exists(self, key: str) -> bool:
        self._check()
        return key in self.objects


class FakeRepository:
    """Records which sessions were reported; can be told to fail."""

    def __init__(self) -> None:
        self.recorded: list[str] = []
        self.redactions: list[tuple[str, list[RedactionRecord]]] = []
        self.environments: list[tuple[str, EnvironmentRecord]] = []
        self.lifecycle: list[tuple[str, LifecycleEvent]] = []
        self.run_keys: dict[str, tuple[str, int]] = {}  # run key -> (chat, seq)
        self.down = False

    def record_chunk(self, session_id: str) -> None:
        if self.down:
            raise RepositoryError("down")
        self.recorded.append(session_id)

    def record_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        if self.down:
            raise RepositoryError("down")
        self.redactions.append((session_id, list(records)))

    def record_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        if self.down:
            raise RepositoryError("down")
        self.environments.append((session_id, record))

    def record_lifecycle(self, session_id: str, event: LifecycleEvent) -> int:
        if self.down:
            raise RepositoryError("down")
        chat, seq = self.run_keys.get(event.run_key, (session_id, len(self.run_keys) + 1))
        if chat != session_id:
            raise RunKeyConflictError("the run key belongs to another chat")
        self.run_keys[event.run_key] = (chat, seq)
        self.lifecycle.append((session_id, event))
        return seq

    def ping(self) -> None:
        if self.down:
            raise RepositoryError("down")


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(store: FakeStore, repository: FakeRepository) -> TestClient:
    service = IngestService(
        token=Secret(TOKEN), store=store, repository=repository, max_chunk_bytes=1024
    )
    return TestClient(service.build_app())
