"""The ingestion API against a real PostgreSQL and a real MinIO (see conftest.py)."""

import hashlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from ccp.db.connection import connect
from ccp.ingest.app import IngestService
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings, Secret
from ccp.storage import ObjectStore
from ccp.storage.attachments import MIN_EXTERNAL_BYTES, externalise_line, restore_line

pytestmark = pytest.mark.integration

REAL_MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
TOKEN = "integration" + "-test-token"  # invented; built from pieces for the secret scanner
AUTH = {"Authorization": f"Bearer {TOKEN}"}
LINE = b'{"type":"user","uuid":"u1"}\n'


def _url(session: str = "sess-1", stream: str = "main", offset: int = 0) -> str:
    return f"/v1/sessions/{session}/streams/{stream}/chunks/{offset}"


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    """Run a query in the throwaway database and return its rows."""
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _execute(database: DatabaseSettings, sql: str) -> None:
    connection = connect(database)
    try:
        connection.cursor().execute(sql)
        connection.commit()
    finally:
        connection.close()


def _service(database: DatabaseSettings, store: ObjectStore) -> IngestService:
    return IngestService(
        token=Secret(TOKEN),
        store=store,
        repository=PostgresRepository(database),
        max_chunk_bytes=1024 * 1024,
    )


@pytest.fixture
def client(database: DatabaseSettings, object_store: ObjectStore) -> TestClient:
    return TestClient(_service(database, object_store).build_app())


# ---------------------------------------------------------------- what an upload leaves behind


def test_an_upload_stores_the_chunk_creates_the_session_and_queues_one_job(
    client: TestClient, database: DatabaseSettings, object_store: ObjectStore
) -> None:
    response = client.put(_url(), content=LINE, headers=AUTH)

    assert response.status_code == 201
    key = "sessions/sess-1/main/000000000000.jsonl"
    assert object_store.get(key) == LINE
    assert _rows(database, "SELECT session_id, status FROM session") == [("sess-1", "open")]
    assert _rows(database, "SELECT job_type, session_id, status FROM job") == [
        ("index_session", "sess-1", "queued")
    ]


def test_many_chunks_of_one_session_queue_a_single_job(
    client: TestClient, database: DatabaseSettings, object_store: ObjectStore
) -> None:
    for offset in (0, 100, 200):
        assert client.put(_url(offset=offset), content=LINE, headers=AUTH).status_code == 201

    assert len(object_store.list_keys("sessions/sess-1/")) == 3
    assert _rows(database, "SELECT count(*) FROM job WHERE status = 'queued'") == [(1,)]
    assert _rows(database, "SELECT count(*) FROM session") == [(1,)]


def test_the_same_chunk_twice_stores_one_object_and_one_job(
    client: TestClient, database: DatabaseSettings, object_store: ObjectStore
) -> None:
    assert client.put(_url(), content=LINE, headers=AUTH).status_code == 201
    assert client.put(_url(), content=LINE, headers=AUTH).status_code == 200  # a retry

    assert len(object_store.list_keys("sessions/")) == 1
    assert _rows(database, "SELECT count(*) FROM job") == [(1,)]


def test_a_new_chunk_queues_a_new_job_once_the_previous_one_is_running(
    client: TestClient, database: DatabaseSettings
) -> None:
    client.put(_url(offset=0), content=LINE, headers=AUTH)
    _execute(database, "UPDATE job SET status = 'running'")  # the indexer picked it up
    client.put(_url(offset=100), content=LINE, headers=AUTH)

    assert sorted(_rows(database, "SELECT status FROM job")) == [("queued",), ("running",)]


def test_different_sessions_get_their_own_rows_and_jobs(
    client: TestClient, database: DatabaseSettings
) -> None:
    client.put(_url(session="a"), content=LINE, headers=AUTH)
    client.put(_url(session="b"), content=LINE, headers=AUTH)

    assert _rows(database, "SELECT session_id FROM session ORDER BY 1") == [("a",), ("b",)]
    assert _rows(database, "SELECT count(*) FROM job") == [(2,)]


def test_a_late_chunk_reopens_a_closed_session(
    client: TestClient, database: DatabaseSettings
) -> None:
    client.put(_url(offset=0), content=LINE, headers=AUTH)
    _execute(
        database,
        "UPDATE session SET status = 'closed', last_activity_at = now() - interval '1 day'",
    )

    client.put(_url(offset=100), content=LINE, headers=AUTH)

    assert _rows(database, "SELECT status FROM session") == [("open",)]
    assert _rows(
        database, "SELECT now() - last_activity_at < interval '1 minute' FROM session"
    ) == [(True,)]


def test_a_sub_agent_stream_belongs_to_the_same_session(
    client: TestClient, database: DatabaseSettings, object_store: ObjectStore
) -> None:
    client.put(_url(stream="main"), content=LINE, headers=AUTH)
    client.put(_url(stream="agent-a7c5c9"), content=LINE, headers=AUTH)

    assert len(object_store.list_keys("sessions/sess-1/")) == 2
    assert _rows(database, "SELECT count(*) FROM session") == [(1,)]
    assert _rows(database, "SELECT count(*) FROM job") == [(1,)]


# ---------------------------------------------------------------- failure and recovery


def test_health_with_real_services(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": "ok", "storage": "ok"}


def test_unreachable_database_gives_503_and_a_later_retry_heals(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    broken = replace(database, password=Secret("wrong-password"))
    broken_client = TestClient(_service(broken, object_store).build_app())
    assert broken_client.get("/health").json()["database"] == "unavailable"
    assert broken_client.put(_url(), content=LINE, headers=AUTH).status_code == 503
    assert object_store.exists("sessions/sess-1/main/000000000000.jsonl")  # chunk saved anyway
    assert _rows(database, "SELECT count(*) FROM session") == [(0,)]  # but not recorded yet

    healed = TestClient(_service(database, object_store).build_app())  # the database is back
    assert healed.put(_url(), content=LINE, headers=AUTH).status_code == 200
    assert _rows(database, "SELECT session_id FROM session") == [("sess-1",)]
    assert _rows(database, "SELECT count(*) FROM job") == [(1,)]


# ---------------------------------------------------------------- over a real network socket


@pytest.fixture
def running_server(database: DatabaseSettings, object_store: ObjectStore) -> Iterator[str]:
    """Run the API with uvicorn on a free local port; yield its base URL."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            _service(database, object_store).build_app(),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_the_api_works_over_real_http(
    running_server: str, database: DatabaseSettings, object_store: ObjectStore
) -> None:
    assert httpx.get(f"{running_server}/health").json()["status"] == "ok"

    url = running_server + _url()
    first = httpx.put(url, content=LINE, headers=AUTH)
    retry = httpx.put(url, content=LINE, headers=AUTH)
    refused = httpx.put(url, content=LINE)  # no token

    assert (first.status_code, retry.status_code, refused.status_code) == (201, 200, 401)
    assert first.json() == {"stored": True}
    assert len(object_store.list_keys("sessions/")) == 1
    assert _rows(database, "SELECT status FROM job") == [("queued",)]


# ---------------------------------------------------------------- attachments end to end


def test_attachments_uploaded_through_the_api_restore_the_original_line(
    client: TestClient, object_store: ObjectStore
) -> None:
    """Pointer lines made on a 'client' restore from the server's store (the formats agree)."""
    body = {"type": "skill_listing", "text": "x" * (MIN_EXTERNAL_BYTES * 2)}
    original = json.dumps(
        {"type": "attachment", "uuid": "u1", "sessionId": "s1", "attachment": body},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    # The "client": externalise into a local dictionary; it never sees the object store.
    local = _DictStore()
    pointer_line = externalise_line(original, local).text
    assert len(pointer_line) < len(original) / 5

    # Upload every body to the API, as the client will.
    for key, data in local.objects.items():
        digest = key.rsplit("/", 1)[1]
        assert (
            client.put(f"/v1/attachments/{digest}", content=data, headers=AUTH).status_code == 201
        )

    # The "server side": the real object store can rebuild the original, byte for byte.
    assert restore_line(pointer_line, object_store) == original
    assert len(object_store.list_keys("attachments/")) == 1


def test_a_second_upload_of_the_same_attachment_is_not_stored_again(
    client: TestClient, object_store: ObjectStore
) -> None:
    data = json.dumps({"k": "v" * 50}, separators=(",", ":")).encode()
    url = f"/v1/attachments/{hashlib.sha256(data).hexdigest()}"
    assert client.put(url, content=data, headers=AUTH).status_code == 201
    assert client.put(url, content=data, headers=AUTH).status_code == 200
    assert len(object_store.list_keys("attachments/")) == 1


class _DictStore:
    """A local dictionary standing in for what a developer's machine has (no object store)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_if_absent(self, key: str, data: bytes) -> bool:
        return self.objects.setdefault(key, data) is data

    def get(self, key: str) -> bytes:
        return self.objects[key]


# ---------------------------------------------------------------- snapshot commits (shadow commits)

SHA = "a" * 40


def _snapshot(**changes: Any) -> dict[str, Any]:
    record = {
        "sha": SHA, "parent_sha": "b" * 40, "actor": "ai_edit", "captured_at": "2026-11-16T10:00:00+00:00",
        "files": [{"path": "src/a.py", "state": "M"}], "files_total": 1, "seq": 3, "event": "PostToolUse",
        "chain": "ccp/dev/host/main", "model": "claude-opus-5-5", "tool": "Edit",
        "tool_use_id": "toolu_1", "prompt_uuid": "p1", "prompt_excerpt": "fix the bug", "agent": "main",
    }  # fmt: skip
    return {**record, **changes}


def test_a_snapshot_is_stored_once_and_creates_its_session(
    client: TestClient, database: DatabaseSettings
) -> None:
    first = client.post("/v1/sessions/sess-snap/snapshots", json=_snapshot(), headers=AUTH)
    again = client.post("/v1/sessions/sess-snap/snapshots", json=_snapshot(), headers=AUTH)
    assert first.status_code == again.status_code == 200
    assert first.json()["new"] is True and again.json()["new"] is False

    rows = _rows(
        database,
        "SELECT sha, session_id, actor, model, tool_use_id, prompt_uuid, chain, files::text, "
        "files_total FROM shadow_commit",
    )
    assert len(rows) == 1 and rows[0][:7] == (
        SHA, "sess-snap", "ai_edit", "claude-opus-5-5", "toolu_1", "p1", "ccp/dev/host/main",
    )  # fmt: skip
    assert json.loads(rows[0][7]) == [{"path": "src/a.py", "state": "M"}] and rows[0][8] == 1


@pytest.mark.parametrize(
    "bad",
    [{"sha": "nothex"}, {"actor": "robot"}, {"captured_at": "yesterday"}, {"files": "x"},
     {"files": [{"path": "a", "state": "Z"}]}, {"model": "bad\nmodel"}, {"files_total": 0}],
)  # fmt: skip
def test_a_malformed_snapshot_is_refused_without_repeating_it(
    client: TestClient, database: DatabaseSettings, bad: dict[str, Any]
) -> None:
    response = client.post("/v1/sessions/sess-snap/snapshots", json=_snapshot(**bad), headers=AUTH)
    assert response.status_code == 422 and "bad" not in response.text
    assert _rows(database, "SELECT 1 FROM shadow_commit") == []


def test_a_snapshot_needs_the_token(client: TestClient) -> None:
    assert client.post("/v1/sessions/sess-snap/snapshots", json=_snapshot()).status_code == 401
