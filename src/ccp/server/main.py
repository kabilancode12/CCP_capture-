"""The ``ccp-server`` command (also ``python -m ccp.server``): everything the server does.

One process, started once and left running:

* the ingestion API (``ccp.ingest``), with its health endpoint and idle-session closer unchanged;
* the indexer worker (``ccp.index``) in a background thread, so uploaded chats are indexed at once;
* the plug-ins that are switched on (catalog, decisions, explorer; see :mod:`ccp.server.plugins`).

    ccp-server                       serve (checks the database schema first)
    ccp-server --migrate             apply pending migrations first, then serve
    ccp-server --skip-schema-check   serve even if migrations are pending

On SIGTERM or Ctrl-C the web server stops accepting requests, the indexer finishes the job it is
on, and the process exits. Settings come from the environment (see docs/configuration.md).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.routing import Mount

from ccp.db.connection import connect
from ccp.db.migrations import MigrationError, discover_migrations, migrate
from ccp.index.repository import IndexRepository, IndexRepositoryError
from ccp.index.worker import ChunkStore, JobRepository
from ccp.ingest.app import IngestService
from ccp.ingest.closer import check_interval_seconds, closer_lifespan
from ccp.ingest.repository import PostgresRepository, RepositoryError
from ccp.ingest.server import schema_problem
from ccp.server.indexer import IndexerThread
from ccp.server.plugins import PluginContext, PluginRegistry, is_enabled, load_plugins
from ccp.settings import DEFAULT_ENV_FILE, Settings, SettingsError, _merge_env, load_settings
from ccp.storage import ObjectStore, StorageError, connect_store

logger = logging.getLogger("ccp.server")

DEFAULT_MIGRATIONS_DIR = Path("db/migrations")
DEFAULT_POLL_SECONDS = 5.0
GRACEFUL_SHUTDOWN_SECONDS = 30


def build_server_app(
    service: IngestService,
    *,
    closer: Callable[[Starlette], AbstractAsyncContextManager[None]],
    indexer: IndexerThread,
    registry: PluginRegistry,
) -> Starlette:
    """Create the one application: the API, with the indexer and closer running while it is up.

    Args:
        service: The ingestion API's handlers.
        closer: The idle-session closer's lifespan (see :func:`ccp.ingest.closer.closer_lifespan`).
        indexer: The indexer thread; started with the server and stopped when it shuts down.
        registry: The plug-ins' mounts are served next to the API routes.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with closer(app):
            indexer.start()
            logger.info("indexer started; plug-ins: %s", ", ".join(registry.loaded) or "none")
            try:
                yield
            finally:
                logger.info("shutting down: finishing the current indexing job")
                await run_in_threadpool(indexer.stop)

    app = service.build_app(lifespan=lifespan)
    for path, sub_app in registry.mounts:
        app.router.routes.append(Mount(path, app=sub_app))
    return app


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="ccp-server", description="Ingestion API + indexer + plug-ins in one process."
    )
    parser.add_argument("--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR)
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="apply pending migrations before serving (also: CCP_SERVER_MIGRATE=true)",
    )
    parser.add_argument(
        "--skip-schema-check", action="store_true", help="start even if migrations are pending"
    )
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser


def _build(settings: Settings, store: ObjectStore, env: dict[str, str], poll: float) -> Starlette:
    """Wire the real repositories, the store and the plug-ins into the application."""
    registry = PluginRegistry()
    load_plugins(registry, PluginContext(settings=settings, store=store, env=env))
    api = settings.api
    service = IngestService(
        token=api.ingest_token,
        store=store,
        repository=PostgresRepository(settings.database),
        max_chunk_bytes=api.max_chunk_bytes,
    )
    closer = closer_lifespan(
        PostgresRepository(settings.database),
        api.session_idle_minutes,
        check_interval_seconds(api.session_idle_minutes),
    )
    index_store: ChunkStore = store
    index_repository: JobRepository = IndexRepository(settings.database)
    indexer = IndexerThread(
        index_store, index_repository, poll_seconds=poll, callbacks=registry.after_index
    )
    return build_server_app(service, closer=closer, indexer=indexer, registry=registry)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the server.

    Returns:
        0 after a clean shutdown, 1 if the settings, the database or the storage failed.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
        env = _merge_env(None, DEFAULT_ENV_FILE)
        if args.migrate or is_enabled(env, "CCP_SERVER_MIGRATE"):
            _apply_migrations(settings, args.migrations_dir)
        if not args.skip_schema_check:
            problem = schema_problem(settings.database, args.migrations_dir)
            if problem:
                print(problem, file=sys.stderr)
                return 1
        app = _build(settings, connect_store(settings.storage), env, args.poll_seconds)
    except (
        SettingsError,
        StorageError,
        RepositoryError,
        IndexRepositoryError,
        MigrationError,
    ) as e:
        print(e, file=sys.stderr)
        return 1
    uvicorn.run(
        app,
        host=settings.api.host,
        port=settings.api.port,
        log_level="info",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return 0


def _apply_migrations(settings: Settings, migrations_dir: Path) -> None:
    """Apply every pending migration (a no-op if the database is current)."""
    migrations = discover_migrations(migrations_dir)
    connection = connect(settings.database)
    try:
        done = migrate(connection, migrations)
    finally:
        connection.close()
    logger.info("migrations applied: %s", ", ".join(m.label for m in done) or "none pending")
