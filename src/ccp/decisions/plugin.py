"""Plug the decisions into ``ccp-server``: a background sweep that processes finished chats.

Asking a model is slow and costs tokens, so a chat is not processed after every prompt. It is
processed when **a session of the chat has ended** (the ``SessionEnd`` hook, recorded in
``chat_session.ended_at`` after the chat's last message), and, for sessions that never send an end
(a closed window, a crash), when the chat has been quiet for ``CCP_DECISION_IDLE_MINUTES`` (default
15). Every ``CCP_DECISION_SWEEP_MINUTES`` (default 2) the sweep takes the chats that need decisions
(:func:`ccp.decisions.repository.pending_chats`) and processes those that are finished, one at a
time. A chat that continues later is processed again: its newer messages make it pending.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ccp.db.connection import connect
from ccp.decisions.provider import choose_provider
from ccp.decisions.repository import chat_state, pending_chats
from ccp.decisions.runner import process_chat
from ccp.decisions.settings import load_decision_settings

if TYPE_CHECKING:
    from ccp.server.plugins import PluginContext, PluginRegistry
    from ccp.settings import DatabaseSettings

logger = logging.getLogger("ccp.decisions")


# Chats whose last run failed, and when: they are left alone for a while.
_FAILED: dict[str, datetime] = {}
_RETRY_AFTER = timedelta(minutes=30)


def _minutes(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name, "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else default


_SESSION_ENDED = """
    SELECT EXISTS (
        SELECT 1 FROM chat_session cs
         WHERE cs.session_id = %s AND cs.run_key IS NOT NULL AND cs.ended_at IS NOT NULL
           AND cs.ended_at >= COALESCE(
               (SELECT max(e.occurred_at) FROM event e WHERE e.session_id = %s),
               cs.ended_at) - interval '1 minute')
"""


def session_ended(connection: Any, session_id: str) -> bool:
    """True if a session of the chat ended after the chat's last message."""
    cursor = connection.cursor()
    cursor.execute(_SESSION_ENDED, (session_id, session_id))
    row = cursor.fetchone()
    connection.rollback()
    return bool(row and row[0])


def sweep_once(
    database: DatabaseSettings,
    idle: timedelta,
    stop: threading.Event,
    env: Mapping[str, str],
    after_chat: Callable[[str], None] | None = None,
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
            failed_at = _FAILED.get(session_id)
            if failed_at is not None and datetime.now(UTC) - failed_at < _RETRY_AFTER:
                continue  # a failed chat is not tried at every sweep: each try costs tokens
            state = chat_state(connection, session_id)
            if state is None or state.is_empty or not state.has_prompt:
                continue
            quiet = state.last_event_at is None or datetime.now(UTC) - state.last_event_at >= idle
            if not quiet and not session_ended(connection, session_id):
                continue
            try:
                process_chat(connection, provider, settings, session_id)
                done += 1
                if after_chat is not None:
                    after_chat(session_id)  # e.g. rebuild the chat's catalog with its decisions
            except Exception as error:  # noqa: BLE001  (one chat must not stop the sweep)
                connection.rollback()
                _FAILED[session_id] = datetime.now(UTC)
                logger.warning(
                    "decisions for one chat failed: %s", error
                )  # a safe message, no chat text
    finally:
        connection.close()
    return done


def register(registry: PluginRegistry, context: PluginContext) -> None:
    """Start the sweep thread (a daemon, so it never keeps the server from stopping)."""
    env = dict(context.env)
    idle = timedelta(minutes=_minutes(env, "CCP_DECISION_IDLE_MINUTES", 15))
    every = _minutes(env, "CCP_DECISION_SWEEP_MINUTES", 2) * 60
    stop = threading.Event()
    # The catalog is built right after indexing, before the decisions exist: rebuild a chat's
    # catalog once its decisions are written, so the readable files are complete.
    catalog_on = "catalog" in registry.loaded

    def rebuild_catalog(session_id: str) -> None:
        if not catalog_on:
            return
        from ccp.catalog.builder import catalog_chat

        connection = connect(context.settings.database)
        try:
            catalog_chat(connection, context.store, session_id)
        except Exception as error:  # noqa: BLE001  (the decisions are stored; the catalog can wait)
            logger.warning("catalog after decisions failed (%s)", type(error).__name__)
        finally:
            connection.close()

    def loop() -> None:
        while not stop.wait(every):
            try:
                sweep_once(context.settings.database, idle, stop, env, rebuild_catalog)
            except Exception as error:  # noqa: BLE001
                logger.warning("decision sweep failed (%s)", type(error).__name__)

    threading.Thread(target=loop, name="ccp-decisions", daemon=True).start()
