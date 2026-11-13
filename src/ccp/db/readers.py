"""A read-only database role for dashboards.

A dashboard tool should not connect as the application's owner: it only needs to *read counts*. The
role made here can read the dashboard views of migration 013 (``d_session`` and the others) and
nothing else: not the event table with its raw transcript lines, not the environment record with its
names of people and machines, not the audit views with prompts and replies.

Running it again is safe: it brings an existing role up to date (new password, current grants).
"""

from __future__ import annotations

import re

import pg8000.dbapi

DASHBOARD_VIEWS = ("d_session", "d_model_cost", "d_edit", "d_test_run", "d_fix_loop")
DEFAULT_READER = "ccp_reader"

_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,30}")


class ReaderError(ValueError):
    """Raised for a role name or password that cannot be used (the message omits the password)."""


def create_reader(
    connection: pg8000.dbapi.Connection, database: str, password: str, name: str = DEFAULT_READER
) -> None:
    """Create (or update) the read-only role and give it the dashboard views.

    Args:
        connection: A connection that may create roles and grant (the application's owner).
        database: The name of the database the role may connect to.
        password: The role's password.
        name: The role's name: lower-case letters, digits and underscores.

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
    for view in DASHBOARD_VIEWS:
        cursor.execute(f"GRANT SELECT ON {view} TO {role}")
    connection.commit()


def _quote(identifier: str) -> str:
    """Quote an identifier for SQL."""
    return '"' + identifier.replace('"', '""') + '"'
