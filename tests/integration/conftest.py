"""Fixtures for tests that need the local PostgreSQL and MinIO (``docker compose up -d``).

Each test gets its own throwaway database or bucket, created before and removed after, so tests
never touch the real ``ccp`` database or ``ccp-raw`` bucket and never interfere with each other.
If a service cannot be reached, the tests that need it are skipped, not failed.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from minio import Minio

from ccp.db.connection import connect
from ccp.settings import (
    DatabaseSettings,
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
