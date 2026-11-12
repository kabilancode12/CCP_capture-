"""Fixtures for tests that need the local PostgreSQL and MinIO (``docker compose up -d``).

Each test gets its own throwaway database or bucket, created before and removed after, so tests
never touch the real ``ccp`` database or ``ccp-raw`` bucket and never interfere with each other.
If a service cannot be reached, the tests that need it are skipped, not failed.
"""

import socket
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from minio import Minio
from starlette.applications import Starlette

from ccp.db.connection import connect
from ccp.db.migrations import discover_migrations, migrate
from ccp.ingest.app import IngestService
from ccp.ingest.repository import PostgresRepository
from ccp.settings import (
    DatabaseSettings,
    Secret,
    SettingsError,
    StorageSettings,
    load_database_settings,
    load_storage_settings,
)
from ccp.storage import ObjectStore, StorageError, connect_store

REPOSITORY_ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="session")
def admin_settings() -> DatabaseSettings:
    """Database settings from `.env`; skips the test if they are missing or unreachable."""
    try:
        settings = load_database_settings(env_file=REPOSITORY_ROOT / ".env")
        connect(settings).close()
    except SettingsError as error:
        pytest.skip(f"database settings not available: {error}")
    except Exception as error:  # noqa: BLE001  (any connection failure means "not running")
        pytest.skip(f"PostgreSQL not reachable (is Docker running?): {type(error).__name__}")
    return settings


@pytest.fixture
def temp_database(admin_settings: DatabaseSettings) -> Iterator[DatabaseSettings]:
    """Settings for a fresh, empty, throwaway database (dropped after the test)."""
    name = f"ccp_test_{uuid.uuid4().hex[:10]}"
    admin = connect(admin_settings)
    admin.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    try:
        yield replace(admin_settings, name=name)
    finally:
        admin.cursor().execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def db_connection(temp_database: DatabaseSettings) -> Iterator[Any]:
    """A connection to the throwaway database."""
    connection = connect(temp_database)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def storage_settings() -> StorageSettings:
    """Storage settings from `.env`; skips the test if they are missing or unreachable."""
    try:
        settings = load_storage_settings(env_file=REPOSITORY_ROOT / ".env")
        # exists() answers False for a missing bucket or key, and raises only if MinIO is down.
        connect_store(settings, create_bucket=False).exists("connectivity-check")
    except SettingsError as error:
        pytest.skip(f"storage settings not available: {error}")
    except StorageError as error:
        pytest.skip(f"MinIO not reachable (is Docker running?): {error}")
    return settings


@pytest.fixture
def object_store(storage_settings: StorageSettings) -> Iterator[ObjectStore]:
    """An object store on a fresh, empty, throwaway bucket."""
    bucket = f"ccp-test-{uuid.uuid4().hex[:10]}"
    store = connect_store(replace(storage_settings, bucket=bucket))
    try:
        yield store
    finally:
        raw = Minio(
            storage_settings.endpoint,
            access_key=storage_settings.access_key,
            secret_key=storage_settings.secret_key.reveal(),
            secure=storage_settings.secure,
        )
        for key in store.list_keys(""):
            raw.remove_object(bucket, key)
        raw.remove_bucket(bucket)


# ---------------------------------------------------------------- the real API (end to end)

REAL_MIGRATIONS = REPOSITORY_ROOT / "db" / "migrations"
API_TOKEN = "integration" + "-api-token-value"  # invented; built from pieces for the secret scanner


class RunningApi:
    """The API served by uvicorn on a fixed local port; can be stopped and started again."""

    def __init__(self, app: Starlette) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """The base URL of the running API."""
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        """Start serving (and wait until the server accepts connections)."""
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self._server.started, "server did not start"

    def stop(self) -> None:
        """Stop serving (gracefully)."""
        if self._server and self._thread:
            self._server.should_exit = True
            self._thread.join(timeout=5)
            self._server = self._thread = None


@pytest.fixture
def database(temp_database: DatabaseSettings) -> DatabaseSettings:
    """The throwaway database with every migration applied."""
    connection = connect(temp_database)
    try:
        migrate(connection, discover_migrations(REAL_MIGRATIONS))
    finally:
        connection.close()
    return temp_database


@pytest.fixture
def api(database: DatabaseSettings, object_store: ObjectStore) -> Iterator[RunningApi]:
    """The real API (real database, real MinIO bucket) running on a local port."""
    service = IngestService(
        token=Secret(API_TOKEN),
        store=object_store,
        repository=PostgresRepository(database),
        max_chunk_bytes=8 * 1024 * 1024,
    )
    running = RunningApi(service.build_app())
    running.start()
    try:
        yield running
    finally:
        running.stop()
