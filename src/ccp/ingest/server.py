"""The ``ccp-ingest`` command (also ``python -m ccp.ingest``).

    ccp-ingest              serve the API (the default)
    ccp-ingest serve        the same, spelled out
    ccp-ingest close-idle   close idle sessions once, then exit (for a scheduler, or by hand)

Settings come from the environment (see docs/configuration.md). The server connects to MinIO
(creating the bucket if needed) and also closes idle sessions in the background. The database must
already have its tables: run ``ccp-db migrate`` first.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from starlette.applications import Starlette

from ccp.db.connection import connect
from ccp.db.migrations import MigrationError, discover_migrations, pending_migrations
from ccp.ingest.app import IngestService
from ccp.ingest.closer import check_interval_seconds, closer_lifespan
from ccp.ingest.repository import PostgresRepository, RepositoryError
from ccp.settings import DatabaseSettings, Settings, SettingsError, load_settings
from ccp.storage import StorageError, connect_store

DEFAULT_MIGRATIONS_DIR = Path("db/migrations")


def schema_problem(database: DatabaseSettings, migrations_dir: Path) -> str | None:
    """Say why the database schema is not ready for this server, or ``None`` if it is.

    A server that starts against a database that is behind its code fails later and confusingly
    (for example "column does not exist" while saving data). This check turns that into one clear
    message at start-up.

    If the migrations folder is not there, or the database cannot be reached, the check is
    skipped with a warning: the server can still start, and ``/health`` reports a database that
    is down.

    Args:
        database: The database settings.
        migrations_dir: The folder of numbered migration files.
    """
    try:
        migrations = discover_migrations(migrations_dir)
    except MigrationError as error:
        logging.getLogger("ccp.ingest").warning("schema check skipped: %s", error)
        return None
    try:
        connection = connect(database)
    except Exception as error:  # noqa: BLE001  (any failure to connect means "cannot check")
        logging.getLogger("ccp.ingest").warning(
            "schema check skipped: database unreachable (%s)", type(error).__name__
        )
        return None
    try:
        pending = pending_migrations(connection, migrations)
    except MigrationError as error:
        return f"The database and the migration files disagree: {error}"
    finally:
        connection.close()
    if pending:
        names = ", ".join(m.label for m in pending)
        return f"The database schema is behind this server (pending: {names}). Run: ccp-db migrate"
    return None


def build_app(settings: Settings) -> Starlette:
    """Create the application, with the idle-session closer running in the background.

    Args:
        settings: The validated settings.

    Raises:
        StorageError: If the object store cannot be reached.
    """
    repository = PostgresRepository(settings.database)
    idle_minutes = settings.api.session_idle_minutes
    service = IngestService(
        token=settings.api.ingest_token,
        store=connect_store(settings.storage),
        repository=repository,
        max_chunk_bytes=settings.api.max_chunk_bytes,
    )
    lifespan = closer_lifespan(repository, idle_minutes, check_interval_seconds(idle_minutes))
    return service.build_app(lifespan=lifespan)


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-ingest", description="Ingestion API.")
    commands = parser.add_subparsers(dest="command")  # no command means "serve"
    serve = commands.add_parser("serve", help="serve the API (default)")
    serve.add_argument("--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR)
    serve.add_argument(
        "--skip-schema-check", action="store_true", help="start even if migrations are pending"
    )
    commands.add_parser("close-idle", help="close idle sessions once, then exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Returns:
        0 on success, 1 if the settings or a service (storage, database) failed.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
        if args.command == "close-idle":
            closed = PostgresRepository(settings.database).close_idle_sessions(
                settings.api.session_idle_minutes
            )
            print(
                f"Closed {len(closed)} idle session(s)"
                + (": " + ", ".join(closed) if closed else "")
            )
            return 0
        if not getattr(args, "skip_schema_check", False):
            problem = schema_problem(
                settings.database, getattr(args, "migrations_dir", DEFAULT_MIGRATIONS_DIR)
            )
            if problem:
                print(problem, file=sys.stderr)
                return 1
        app = build_app(settings)
    except (SettingsError, StorageError, RepositoryError) as error:
        print(error, file=sys.stderr)
        return 1
    uvicorn.run(app, host=settings.api.host, port=settings.api.port, log_level="info")
    return 0
