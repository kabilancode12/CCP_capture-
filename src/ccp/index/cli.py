"""The ``ccp-index`` command (also ``python -m ccp.index``).

    ccp-index run              work through the index jobs and keep waiting for more
    ccp-index run --once       work through the queued jobs, then exit
    ccp-index rebuild ...      delete the index of some sessions and build it again

``rebuild`` takes ``--session ID`` (repeatable) or ``--all``. Without ``--check`` it deletes the
index and queues a job per session, for a running ``run`` to build again. With ``--check`` it builds
again at once and reports whether the result is identical to what was there before (exit code 1 if
not): the proof that the index really can be rebuilt from the stored transcripts.

Settings come from the environment (see docs/configuration.md): the ``POSTGRES_*`` and ``MINIO_*``
variables. The database must already have its tables: run ``ccp-db migrate`` first.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.index.rebuild import RebuildRepository, rebuild_session
from ccp.index.repository import IndexRepository, IndexRepositoryError
from ccp.index.worker import run
from ccp.ingest.server import schema_problem
from ccp.settings import (
    DatabaseSettings,
    SettingsError,
    load_database_settings,
    load_storage_settings,
)
from ccp.storage import ObjectStore, StorageError, connect_store

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

    rebuild = commands.add_parser("rebuild", help="delete the index of sessions and build it again")
    chosen = rebuild.add_mutually_exclusive_group(required=True)
    chosen.add_argument("--session", action="append", metavar="ID", help="a session (repeatable)")
    chosen.add_argument("--all", action="store_true", help="every session")
    rebuild.add_argument(
        "--check", action="store_true", help="build again now and compare with the old index"
    )
    rebuild.add_argument("--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR)
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
        if args.command == "rebuild":
            return _rebuild(args, database, store)
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


def _rebuild(args: argparse.Namespace, database: DatabaseSettings, store: ObjectStore) -> int:
    """Run ``ccp-index rebuild``. Returns 1 if a session is unknown or its rebuild differs."""
    rebuild = RebuildRepository(database)
    sessions = rebuild.session_ids() if args.all else list(dict.fromkeys(args.session))
    unknown = [s for s in sessions if not rebuild.exists(s)]
    if unknown:
        print(f"Unknown session(s): {', '.join(unknown)}", file=sys.stderr)
        return 1

    index = IndexRepository(database)
    problems = 0
    for session_id in sessions:
        result = rebuild_session(store, index, rebuild, session_id, check=args.check)
        if result is None:
            print(f"{session_id}: index deleted, job queued")
        elif result.differences:
            problems += 1
            print(f"{session_id}: DIFFERENT in {', '.join(result.differences)}")
        else:
            print(f"{session_id}: identical ({result.events} events)")
    if args.check:
        print(f"{len(sessions) - problems} of {len(sessions)} session(s) identical")
    return 1 if problems else 0
