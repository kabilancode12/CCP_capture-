"""Fakes and fixtures for the ingestion API unit tests (no database or MinIO needed)."""

import pytest
from starlette.testclient import TestClient

from ccp.ingest.app import IngestService
from ccp.ingest.repository import RepositoryError
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
        self.down = False

    def record_chunk(self, session_id: str) -> None:
        if self.down:
            raise RepositoryError("down")
        self.recorded.append(session_id)

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
