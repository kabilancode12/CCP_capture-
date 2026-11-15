"""Plug the decisions into ``ccp-server``: a background sweep that processes quiet chats.

Asking a model is slow and costs tokens, so a chat is processed only once it has been quiet for
``CCP_DECISION_IDLE_MINUTES`` (default 30), never after every prompt. Every
``CCP_DECISION_SWEEP_MINUTES`` (default 10) the sweep takes the chats that need decisions
(:func:`ccp.decisions.repository.pending_chats`) and processes the quiet ones, one at a time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ccp.db.connection import connect
from ccp.decisions.provider import choose_provider
from ccp.decisions.repository import chat_state, pending_chats
from ccp.decisions.runner import process_chat
from ccp.decisions.settings import load_decision_settings

if TYPE_CHECKING:
    from ccp.server.plugins import PluginContext, PluginRegistry
    from ccp.settings import DatabaseSettings

logger = logging.getLogger("ccp.decisions")


def _minutes(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


def sweep_once(
    database: DatabaseSettings, idle: timedelta, stop: threading.Event, env: Mapping[str, str]
) -> int:
    """Process every pending chat that has been quiet for ``idle``; returns how many were done."""
    settings = load_decision_settings(env)
    provider = choose_provider(settings)
    connection = connect(database)
    done = 0
    try:
        for session_id in pending_chats(connection, False):
            if stop.is_set():
                break
            state = chat_state(connection, session_id)
            if state is None or state.is_empty or not state.has_prompt:
                continue
            if state.last_event_at is not None and datetime.now(UTC) - state.last_event_at < idle:
                continue
            try:
                process_chat(connection, provider, settings, session_id)
                done += 1
            except Exception as error:  # noqa: BLE001  (one chat must not stop the sweep)
                connection.rollback()
                logger.warning("decisions for one chat failed (%s)", type(error).__name__)
    finally:
        connection.close()
    return done


def register(registry: PluginRegistry, context: PluginContext) -> None:
    """Start the sweep thread (a daemon, so it never keeps the server from stopping)."""
    env = dict(context.env)
    idle = timedelta(minutes=_minutes(env, "CCP_DECISION_IDLE_MINUTES", 30))
    every = _minutes(env, "CCP_DECISION_SWEEP_MINUTES", 10) * 60
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(every):
            try:
                sweep_once(context.settings.database, idle, stop, env)
            except Exception as error:  # noqa: BLE001
                logger.warning("decision sweep failed (%s)", type(error).__name__)

    threading.Thread(target=loop, name="ccp-decisions", daemon=True).start()
