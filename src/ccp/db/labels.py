"""Short names for Claude accounts and projects, shown on the dashboards.

The dashboards never show an e-mail address or a full folder path. Instead they show a label that
an administrator chooses: ``claude1`` for a Claude account, ``billing-api`` for a project folder.
Without a label the dashboard view ``d_usage`` (migration 015) shows a neutral name
(``account-3f9a1c``, or the last part of the folder).

Labels are stored in the tables ``account_label`` and ``project_label``. Setting a label again
replaces it; removing it brings the neutral name back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pg8000.dbapi

# The same rule as the CHECK constraint in migration 015: short plain text, never a sentence.
_LABEL = re.compile(r"[A-Za-z0-9 _.-]{1,40}")


class LabelError(ValueError):
    """Raised for a label or key that cannot be used."""


@dataclass(frozen=True)
class Label:
    """One label: what it names (an e-mail address or a folder) and the name shown."""

    kind: str  # account | project
    key: str
    label: str


def set_account_label(connection: pg8000.dbapi.Connection, email: str, label: str) -> None:
    """Give a Claude account (by its e-mail address) a label, replacing an earlier one.

    Raises:
        LabelError: If the e-mail address is empty or the label is not acceptable.
    """
    key = email.strip().lower()
    if not key:
        raise LabelError("The account's e-mail address is empty")
    _execute(
        connection,
        "INSERT INTO account_label (account_email, label) VALUES (%s, %s) "
        "ON CONFLICT (account_email) DO UPDATE SET label = EXCLUDED.label",
        (key, _checked(label)),
    )


def set_project_label(connection: pg8000.dbapi.Connection, folder: str, label: str) -> None:
    """Give a project folder a label, replacing an earlier one.

    Args:
        connection: A connection that may write the label tables.
        folder: The session folder exactly as Claude Code wrote it (``ccp-db labels`` lists them).
        label: The name to show.

    Raises:
        LabelError: If the folder is empty or the label is not acceptable.
    """
    if not folder.strip():
        raise LabelError("The folder is empty")
    _execute(
        connection,
        "INSERT INTO project_label (folder, label) VALUES (%s, %s) "
        "ON CONFLICT (folder) DO UPDATE SET label = EXCLUDED.label",
        (folder, _checked(label)),
    )


def remove_label(connection: pg8000.dbapi.Connection, kind: str, key: str) -> bool:
    """Remove an account or project label. Returns True if one was removed.

    Raises:
        LabelError: If ``kind`` is not ``account`` or ``project``.
    """
    if kind == "account":
        sql, value = "DELETE FROM account_label WHERE account_email = %s", key.strip().lower()
    elif kind == "project":
        sql, value = "DELETE FROM project_label WHERE folder = %s", key
    else:
        raise LabelError("The kind must be 'account' or 'project'")
    cursor = connection.cursor()
    cursor.execute(sql, (value,))
    removed = bool(cursor.rowcount > 0)
    connection.commit()
    return removed


def list_labels(connection: pg8000.dbapi.Connection) -> list[Label]:
    """Every account and every project folder seen in a session, with its label if it has one.

    Accounts and folders without a label are listed with an empty label, so that an administrator
    can see what still needs a name. This shows e-mail addresses and folder paths: it is for
    administrators only.
    """
    cursor = connection.cursor()
    cursor.execute(
        "SELECT 'account', e.email, COALESCE(l.label, '') "
        "  FROM (SELECT DISTINCT lower(account_email) AS email FROM session_env "
        "         WHERE account_email IS NOT NULL "
        "        UNION SELECT account_email FROM account_label) e "
        "  LEFT JOIN account_label l ON l.account_email = e.email "
        "UNION ALL "
        "SELECT 'project', f.folder, COALESCE(l.label, '') "
        "  FROM (SELECT DISTINCT folder FROM v_session_folder WHERE folder IS NOT NULL "
        "        UNION SELECT folder FROM project_label) f "
        "  LEFT JOIN project_label l ON l.folder = f.folder "
        "ORDER BY 1, 2"
    )
    return [Label(str(kind), str(key), str(label)) for kind, key, label in cursor.fetchall()]


def _checked(label: str) -> str:
    """Return the label without outer spaces, or raise if it is not acceptable."""
    text = label.strip()
    if not _LABEL.fullmatch(text):
        raise LabelError("A label is 1 to 40 letters, digits, spaces, dots, dashes or underscores")
    return text


def _execute(connection: pg8000.dbapi.Connection, sql: str, parameters: tuple[str, str]) -> None:
    """Run one write statement and commit it."""
    connection.cursor().execute(sql, parameters)
    connection.commit()
