"""Start the ingestion API: ``ccp-ingest`` (or ``python -m ccp.ingest``).

Reads settings from the environment (see docs/configuration.md), connects to MinIO (creating the
bucket if needed) and serves the API with uvicorn. The database must already have its tables:
run ``ccp-db migrate`` first.
"""

from __future__ import annotations

import logging
import sys

import uvicorn
from starlette.applications import Starlette

from ccp.ingest.app import IngestService
from ccp.ingest.repository import PostgresRepository
from ccp.settings import SettingsError, load_settings
from ccp.storage import StorageError, connect_store


def build_app() -> Starlette:
    """Create the application from the environment settings.

    Raises:
        SettingsError: If a setting is missing or invalid.
        StorageError: If the object store cannot be reached.
    """
    settings = load_settings()
    return IngestService(
        token=settings.api.ingest_token,
        store=connect_store(settings.storage),
        repository=PostgresRepository(settings.database),
        max_chunk_bytes=settings.api.max_chunk_bytes,
    ).build_app()


def main() -> int:
    """Run the server until interrupted. Returns the process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        settings = load_settings()
        app = build_app()
    except (SettingsError, StorageError) as error:
        print(error, file=sys.stderr)
        return 1
    uvicorn.run(app, host=settings.api.host, port=settings.api.port, log_level="info")
    return 0
