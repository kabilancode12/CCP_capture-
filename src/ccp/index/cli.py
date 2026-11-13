"""The ``ccp-index`` command (also ``python -m ccp.index``).

    ccp-index run            work through the index jobs and keep waiting for more
    ccp-index run --once     work through the queued jobs, then exit

Settings come from the environment (see docs/configuration.md): the ``POSTGRES_*`` and ``MINIO_*``
variables. The database must already have its tables: run ``ccp-db migrate`` first.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.index.repository import IndexRepository, IndexRepositoryError
from ccp.index.worker import run
from ccp.ingest.server import schema_problem
from ccp.settings import SettingsError, load_database_settings, load_storage_settings
from ccp.storage import StorageError, connect_store

DEFAULT_MIGRATIONS_DIR = Path("db/migrations")
DEFAULT_POLL_SECONDS = 5.0


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-index", description="Indexer.")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("run", help="index queued sessions")
    worker.add_argument("--once", action="store_true", help="exit when the queue is empty")
    worker.add_argument(
        "--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="wait when idle"
    )
    worker.add_argument("--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Returns:
        0 on success, 1 if the settings, the database or the storage failed.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        database = load_database_settings()
        storage = load_storage_settings()
        problem = schema_problem(database, args.migrations_dir)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        store = connect_store(storage)
        handled = run(
            store, IndexRepository(database), poll_seconds=args.poll_seconds, once=args.once
        )
    except (SettingsError, StorageError, IndexRepositoryError) as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(f"Handled {handled} job(s)")
    return 0
