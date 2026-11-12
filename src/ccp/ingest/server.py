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

import uvicorn
from starlette.applications import Starlette

from ccp.ingest.app import IngestService
from ccp.ingest.closer import check_interval_seconds, closer_lifespan
from ccp.ingest.repository import PostgresRepository, RepositoryError
from ccp.settings import Settings, SettingsError, load_settings
from ccp.storage import StorageError, connect_store


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
    commands.add_parser("serve", help="serve the API (default)")
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
        app = build_app(settings)
    except (SettingsError, StorageError, RepositoryError) as error:
        print(error, file=sys.stderr)
        return 1
    uvicorn.run(app, host=settings.api.host, port=settings.api.port, log_level="info")
    return 0
