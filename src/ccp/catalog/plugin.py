"""Plug the catalog into ``ccp-server``: rebuild a chat's folder after the chat was indexed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import threading

from ccp.catalog.builder import catalog_all, catalog_chat
from ccp.db.connection import connect

if TYPE_CHECKING:
    from ccp.server.plugins import PluginContext, PluginRegistry

logger = logging.getLogger("ccp.catalog")


def register(registry: PluginRegistry, context: PluginContext) -> None:
    """Add the after-index callback that keeps ``catalog/`` up to date (safe to repeat)."""
    settings, store = context.settings, context.store

    def rebuild(session_id: str) -> None:
        connection = connect(settings.database)
        try:
            # After every turn: refresh report.md and the readable files, not the raw copy.
            catalog_chat(connection, store, session_id, light=True)
        finally:
            connection.close()

    registry.add_after_index(rebuild)

    def rebuild_all() -> None:
        # Once at start: a server that was upgraded writes every folder with the current files
        # (an older version may have written folders without report.md or with older content).
        connection = connect(settings.database)
        try:
            result = catalog_all(connection, store)
            logger.info("catalog rebuilt at start: %d session(s)", len(result.built))
        except Exception as error:  # noqa: BLE001  (the per-turn rebuild still works)
            logger.warning("catalog rebuild at start failed (%s)", type(error).__name__)
        finally:
            connection.close()

    threading.Thread(target=rebuild_all, name="ccp-catalog-start", daemon=True).start()
