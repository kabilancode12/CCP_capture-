"""Open PostgreSQL connections."""

from __future__ import annotations

import pg8000.dbapi

from ccp.settings import DatabaseSettings


def connect(settings: DatabaseSettings, database: str | None = None) -> pg8000.dbapi.Connection:
    """Open a connection to PostgreSQL.

    The connection starts a transaction on the first statement; call ``commit()`` to keep
    changes (or set ``autocommit = True`` for statements such as ``CREATE DATABASE``).

    Args:
        settings: Host, port, user, password and default database name.
        database: Connect to this database instead of ``settings.name`` (used by tests that
            create a throwaway database).

    Returns:
        An open connection. The caller must close it.
    """
    return pg8000.dbapi.connect(
        host=settings.host,
        port=settings.port,
        database=database or settings.name,
        user=settings.user,
        password=settings.password.reveal(),
    )
