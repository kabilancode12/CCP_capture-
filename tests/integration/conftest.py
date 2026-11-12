"""Fixtures for tests that need the local PostgreSQL (``docker compose up -d``).

Each test gets its own throwaway database, created before and dropped after, so tests never
touch the real ``ccp`` database and never interfere with each other. If PostgreSQL cannot be
reached, the tests are skipped, not failed.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ccp.db.connection import connect
from ccp.settings import DatabaseSettings, SettingsError, load_database_settings

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
def db_connection(admin_settings: DatabaseSettings) -> Iterator[Any]:
    """A connection to a fresh, empty, throwaway database."""
    name = f"ccp_test_{uuid.uuid4().hex[:10]}"
    admin = connect(admin_settings)
    admin.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    connection = connect(replace(admin_settings, name=name))
    try:
        yield connection
    finally:
        connection.close()
        admin.cursor().execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.close()
