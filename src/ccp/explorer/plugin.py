"""Plug the Explorer website into ``ccp-server`` under ``/explorer``."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ccp.explorer.app import create_app

if TYPE_CHECKING:
    from ccp.server.plugins import PluginContext, PluginRegistry

logger = logging.getLogger("ccp.explorer")


def register(registry: PluginRegistry, context: PluginContext) -> None:
    """Mount the website; skipped with a warning when ``CCP_EXPLORER_SECRET`` is missing."""
    secret = context.env.get("CCP_EXPLORER_SECRET", "").strip()
    if len(secret) < 16:
        logger.warning("explorer not started: CCP_EXPLORER_SECRET must have 16 or more characters")
        return
    registry.add_mount(
        "/explorer", create_app(context.settings.database, context.store, secret=secret)
    )
