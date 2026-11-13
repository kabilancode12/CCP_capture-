"""Setting up the dashboards in Metabase: first-time setup, database, cards and dashboard.

Every step looks at what already exists and changes only what differs, so
``ccp-dashboards setup`` can be run again after a change to the cards or the layout (or after
the password of the read-only role changed) and always ends in the same state. Things are found
by **name**: a card or dashboard that a person renamed in Metabase is treated as new, and the
old one is left alone.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from ccp.dashboards.client import MetabaseClient, MetabaseError
from ccp.dashboards.definitions import (
    CARDS,
    COLLECTION_NAME,
    DASHBOARD_DESCRIPTION,
    DASHBOARD_NAME,
    Card,
)
from ccp.settings import DashboardSettings

logger = logging.getLogger("ccp.dashboards")

DATABASE_DISPLAY_NAME = "CCP"
SITE_NAME = "CCP dashboards"
# Metabase reads the database's tables in the background after it is added; wait for that.
SYNC_WAIT_SECONDS = 90
SYNC_POLL_SECONDS = 3


def sign_in(client: MetabaseClient, settings: DashboardSettings) -> bool:
    """Make the first-time setup if Metabase has not had one, otherwise sign in.

    Returns:
        True if this call did the first-time setup (and so created the administrator).

    Raises:
        MetabaseError: If Metabase cannot be reached or refuses the sign-in.
    """
    properties = client.request("GET", "/api/session/properties")
    properties = properties if isinstance(properties, dict) else {}
    token = properties.get("setup-token")
    # The token is still offered after the setup, so ask whether an administrator exists.
    if not token or properties.get("has-user-setup") is True:
        client.login(settings.admin_email, settings.admin_password.reveal())
        return False
    answer = client.request(
        "POST",
        "/api/setup",
        {
            "token": token,
            "user": {
                "first_name": "CCP",
                "last_name": "Administrator",
                "email": settings.admin_email,
                "password": settings.admin_password.reveal(),
                "site_name": SITE_NAME,
            },
            "prefs": {"site_name": SITE_NAME, "allow_tracking": False},
        },
    )
    session = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(session, str):
        raise MetabaseError("the first-time setup answered without a session")
    client.use_session(session)
    return True


def ensure_database(client: MetabaseClient, settings: DashboardSettings) -> int:
    """Add the PostgreSQL database (read through the read-only role), or update its connection.

    Returns:
        The id of the database in Metabase.
    """
    details = {
        "host": settings.database_host,
        "port": settings.database_port,
        "dbname": settings.database_name,
        "user": settings.reader_user,
        "password": settings.reader_password.reveal(),
        "ssl": False,
    }
    existing = _find(client.request("GET", "/api/database"), DATABASE_DISPLAY_NAME)
    if existing is None:
        created = client.request(
            "POST",
            "/api/database",
            {"name": DATABASE_DISPLAY_NAME, "engine": "postgres", "details": details},
        )
        return int(created["id"])
    client.request(
        "PUT", f"/api/database/{existing['id']}", {"details": details, "engine": "postgres"}
    )
    return int(existing["id"])


def wait_for_tables(
    client: MetabaseClient,
    database_id: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until Metabase has read the database's views, so that cards can be made on them.

    Raises:
        MetabaseError: If they do not appear in time (usually: the read-only role cannot connect).
    """
    client.request("POST", f"/api/database/{database_id}/sync_schema")
    deadline = clock() + SYNC_WAIT_SECONDS
    while True:
        metadata = client.request("GET", f"/api/database/{database_id}/metadata")
        tables = metadata.get("tables", []) if isinstance(metadata, dict) else []
        if any(table.get("name") == "d_session" for table in tables):
            return
        if clock() >= deadline:
            raise MetabaseError(
                "Metabase did not find the dashboard views; check the read-only role and the "
                "database settings (ccp-db create-reader, CCP_READER_PASSWORD)"
            )
        sleep(SYNC_POLL_SECONDS)


def ensure_collection(client: MetabaseClient) -> int:
    """Find or make the collection that holds the cards and the dashboard."""
    existing = _find(client.request("GET", "/api/collection"), COLLECTION_NAME)
    if existing is not None:
        return int(existing["id"])
    created = client.request("POST", "/api/collection", {"name": COLLECTION_NAME})
    return int(created["id"])


def ensure_cards(client: MetabaseClient, database_id: int, collection_id: int) -> dict[str, int]:
    """Make or update every card of the dashboard.

    Returns:
        The id of each card, by name.
    """
    known = {
        card["name"]: card
        for card in client.request("GET", "/api/card")
        if isinstance(card, dict) and card.get("collection_id") == collection_id
    }
    ids: dict[str, int] = {}
    for card in CARDS:
        body = _card_body(card, database_id, collection_id)
        if card.name in known:
            client.request("PUT", f"/api/card/{known[card.name]['id']}", body)
            ids[card.name] = int(known[card.name]["id"])
        else:
            ids[card.name] = int(client.request("POST", "/api/card", body)["id"])
    return ids


def ensure_dashboard(client: MetabaseClient, collection_id: int, card_ids: dict[str, int]) -> int:
    """Make or update the dashboard so that it holds exactly the cards of the layout.

    Returns:
        The id of the dashboard.
    """
    existing = None
    for dashboard in client.request("GET", "/api/dashboard"):
        if (
            dashboard.get("name") == DASHBOARD_NAME
            and dashboard.get("collection_id") == collection_id
        ):
            existing = dashboard
            break
    body = {"name": DASHBOARD_NAME, "description": DASHBOARD_DESCRIPTION}
    if existing is None:
        dashboard_id = int(
            client.request("POST", "/api/dashboard", {**body, "collection_id": collection_id})["id"]
        )
    else:
        dashboard_id = int(existing["id"])
    # Sending the whole list replaces what was there, so the dashboard always matches the layout.
    client.request(
        "PUT",
        f"/api/dashboard/{dashboard_id}",
        {
            **body,
            "dashcards": [
                {
                    "id": -(index + 1),  # negative: a new placement
                    "card_id": card_ids[card.name],
                    "row": card.row,
                    "col": card.col,
                    "size_x": card.width,
                    "size_y": card.height,
                    "series": [],
                    "parameter_mappings": [],
                    "visualization_settings": {},
                }
                for index, card in enumerate(CARDS)
            ],
        },
    )
    return dashboard_id


def run_setup(client: MetabaseClient, settings: DashboardSettings) -> str:
    """Do every step and return the address of the dashboard."""
    first_time = sign_in(client, settings)
    logger.info("%s", "first-time setup done" if first_time else "signed in")
    database_id = ensure_database(client, settings)
    wait_for_tables(client, database_id)
    collection_id = ensure_collection(client)
    card_ids = ensure_cards(client, database_id, collection_id)
    dashboard_id = ensure_dashboard(client, collection_id, card_ids)
    return f"{settings.metabase_url}/dashboard/{dashboard_id}"


def _card_body(card: Card, database_id: int, collection_id: int) -> dict[str, Any]:
    """The request body that makes or updates one card (a native SQL question)."""
    return {
        "name": card.name,
        "description": card.description or None,
        "display": card.display,
        "collection_id": collection_id,
        "dataset_query": {
            "type": "native",
            "database": database_id,
            "native": {"query": card.sql},
        },
        "visualization_settings": card.settings,
    }


def _find(listing: Any, name: str) -> dict[str, Any] | None:
    """The entry called ``name`` in a list answer (a list, or ``{"data": [...]}``)."""
    entries = listing.get("data", []) if isinstance(listing, dict) else listing
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None
