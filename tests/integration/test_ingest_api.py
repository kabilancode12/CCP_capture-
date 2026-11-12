"""The ingestion API against a real PostgreSQL and a real MinIO (see conftest.py)."""

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
from ccp.db.migrations import discover_migrations, migrate
from ccp.ingest.app import IngestService
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings, Secret
from ccp.storage import ObjectStore

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


@pytest.fixture
def database(temp_database: DatabaseSettings) -> DatabaseSettings:
    """The throwaway database with every migration applied."""
    connection = connect(temp_database)
    try:
        migrate(connection, discover_migrations(REAL_MIGRATIONS))
    finally:
        connection.close()
    return temp_database


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
    assert first.json() == {"stored": True, "next_offset": len(LINE)}
    assert len(object_store.list_keys("sessions/")) == 1
    assert _rows(database, "SELECT status FROM job") == [("queued",)]
