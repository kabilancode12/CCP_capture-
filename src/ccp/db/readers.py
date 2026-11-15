"""Read-only database roles: one for dashboards, one for auditors.

Two levels of access, so that everybody can see usage and only a few people can see what was said:

========  ===========  ====================================================================
Role      Default name What it may read
========  ===========  ====================================================================
reader    ccp_reader   Only the dashboard views (``d_session``, ``d_usage``, ``d_chat`` ...):
                       counts, tokens, costs and short labels. Never prompts, replies, e-mail
                       addresses or names of people and machines. Metabase connects with it,
                       so every developer can open the dashboards.
auditor   ccp_auditor  Every table and view, read-only: the full chat history, the
                       environment record, the audit views. For the few people who must
                       answer "who did what and why".
========  ===========  ====================================================================

Neither role can change anything. Running a command again is safe: it brings an existing role up to
date (new password, current grants). Run ``ccp-db create-auditor`` again after a migration that adds
tables, so that the auditor can read them too, and ``ccp-db create-reader`` again after a migration
that adds or recreates a dashboard view. Migration 016 recreates ``d_usage`` and adds ``d_chat``: it
passes the right to read ``d_usage`` on and grants ``d_chat`` to the same roles, but run both
commands after it all the same.
"""

from __future__ import annotations

import re

import pg8000.dbapi

DASHBOARD_VIEWS = (
    "d_session",
    "d_model_cost",
    "d_edit",
    "d_test_run",
    "d_fix_loop",
    "d_usage",
    "d_chat",
)
DEFAULT_READER = "ccp_reader"
DEFAULT_AUDITOR = "ccp_auditor"

_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,30}")


class ReaderError(ValueError):
    """Raised for a role name or password that cannot be used (the message omits the password)."""


def create_reader(
    connection: pg8000.dbapi.Connection, database: str, password: str, name: str = DEFAULT_READER
) -> None:
    """Create (or update) the read-only role for dashboards and give it the dashboard views.

    Args:
        connection: A connection that may create roles and grant (the application's owner).
        database: The name of the database the role may connect to.
        password: The role's password.
        name: The role's name: lower-case letters, digits and underscores.

    Raises:
        ReaderError: If the name or password is not acceptable.
    """
    cursor = _prepare_role(connection, database, password, name)
    role = _quote(name)
    for view in DASHBOARD_VIEWS:
        cursor.execute(f"GRANT SELECT ON {view} TO {role}")
    connection.commit()


def create_auditor(
    connection: pg8000.dbapi.Connection, database: str, password: str, name: str = DEFAULT_AUDITOR
) -> None:
    """Create (or update) the read-only role for auditors and let it read every table and view.

    Args:
        connection: A connection that may create roles and grant (the application's owner).
        database: The name of the database the role may connect to.
        password: The role's password.
        name: The role's name: lower-case letters, digits and underscores.

    Raises:
        ReaderError: If the name or password is not acceptable.
    """
    cursor = _prepare_role(connection, database, password, name)
    cursor.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_quote(name)}")
    connection.commit()


def _prepare_role(
    connection: pg8000.dbapi.Connection, database: str, password: str, name: str
) -> pg8000.dbapi.Cursor:
    """Create or update a login role with no rights yet, and return a cursor for the grants.

    Raises:
        ReaderError: If the name or password is not acceptable.
    """
    if not _ROLE_NAME.fullmatch(name):
        raise ReaderError("The role name must be lower-case letters, digits and underscores")
    if "\x00" in password:
        raise ReaderError("The password must not contain a NUL character")

    role = _quote(name)
    literal = "'" + password.replace("'", "''") + "'"
    cursor = connection.cursor()
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    verb = "ALTER" if cursor.fetchone() else "CREATE"
    cursor.execute(  # the password cannot be a parameter in this statement
        f"{verb} ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {literal}"
    )
    # Start from nothing, so that running this again also takes away what is no longer wanted.
    cursor.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}")
    cursor.execute(f"GRANT CONNECT ON DATABASE {_quote(database)} TO {role}")
    cursor.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    return cursor


def _quote(identifier: str) -> str:
    """Quote an identifier for SQL."""
    return '"' + identifier.replace('"', '""') + '"'
