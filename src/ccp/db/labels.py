"""Short names for Claude accounts and projects, shown on the dashboards.

The dashboards never show an e-mail address or a full folder path. Instead they show a label that
an administrator chooses: ``claude1`` for a Claude account, ``billing-api`` for a project. Without a
label the dashboard views (``d_usage``, ``d_chat``) show a neutral name (``account-3f9a1c``, or the
project's name: the last part of its Git remote or folder; ``project-3f9a1c`` for a home folder,
whose last part is the person's login name).

Labels are stored on the rows they name (migration 016): ``claude_account.label`` and
``project.label``. Setting a label again replaces it; removing it brings the neutral name back.

* An account is chosen by its e-mail address (or its account uuid). An account seen only in an
  environment record so far gets its row here.
* A project is chosen by its key (``github.com/acme/billing``), by one of its folders, or by its
  name if exactly one project has that name. A folder that no project has yet becomes a project of
  its own (key: the folder); the first chat in that folder takes it over with its label (see
  :mod:`ccp.db.hierarchy`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pg8000.dbapi

from ccp.db.hierarchy import folder_name, normalise_folder

# The same rule as the CHECK constraints in migration 016: short plain text, never a sentence.
_LABEL = re.compile(r"[A-Za-z0-9 _.-]{1,40}")

# An account uuid as Claude Code writes it (oauthAccount.accountUuid).
_ACCOUNT_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

# Projects matched by key or folder first; by name only if nothing matched that way.
_FIND_PROJECTS = """
    SELECT project_id FROM project WHERE project_key = %s OR %s = ANY (folders)
"""

_FIND_ACCOUNT = """
    SELECT account_id FROM claude_account
     WHERE lower(email) = lower(%s) OR account_uuid = %s
     ORDER BY account_uuid IS NULL, account_id
"""

_LIST = """
    SELECT 'account', COALESCE(a.email, a.account_uuid), COALESCE(a.label, ''), ''
      FROM claude_account a
    UNION ALL
    SELECT DISTINCT 'account', lower(env.account_email), '', ''
      FROM session_env env
     WHERE env.account_email IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM claude_account a
                        WHERE lower(a.email) = lower(env.account_email))
    UNION ALL
    SELECT 'project', COALESCE(p.git_remote, p.folders[1], p.project_key), COALESCE(p.label, ''),
           p.name
      FROM project p
    UNION ALL
    SELECT DISTINCT 'project', f.folder, '', ''
      FROM v_session_folder f
     WHERE f.folder IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project p WHERE f.folder = ANY (p.folders))
    ORDER BY 1, 2
"""


class LabelError(ValueError):
    """Raised for a label or key that cannot be used."""


@dataclass(frozen=True)
class Label:
    """One labelled (or not yet labelled) account or project.

    Attributes:
        kind: ``account`` or ``project``.
        key: What to type to name it: the e-mail address (or account uuid), or the project's Git
            remote or first folder.
        label: The name shown on dashboards; empty if none is set.
        name: For a project: its neutral name (the last part of its remote or folder).
    """

    kind: str
    key: str
    label: str
    name: str = ""


def set_account_label(connection: pg8000.dbapi.Connection, email: str, label: str) -> None:
    """Give a Claude account (by its e-mail address or account uuid) a label, replacing an old one.

    An account not yet in ``claude_account`` (seen only in an environment record, or not seen yet)
    is added: by its uuid if ``email`` is an account uuid, otherwise by its e-mail address.

    Raises:
        LabelError: If the key is empty, matches no account and is neither an e-mail address nor
            an account uuid, or the label is not acceptable.
    """
    key = email.strip()
    if not key:
        raise LabelError("The account's e-mail address is empty")
    text = _checked(label)
    cursor = connection.cursor()
    cursor.execute(_FIND_ACCOUNT, (key, key.lower()))  # Claude Code writes uuids in lower case
    found = cursor.fetchone()
    if found is None and _ACCOUNT_UUID.fullmatch(key):
        cursor.execute(
            "INSERT INTO claude_account (account_uuid, label) VALUES (lower(%s), %s)", (key, text)
        )
    elif found is None:
        if "@" not in key:
            raise LabelError("No account has that e-mail address or uuid (see: ccp-db labels)")
        cursor.execute(
            "INSERT INTO claude_account (email, label) VALUES (lower(%s), %s)", (key, text)
        )
    else:
        cursor.execute(
            "UPDATE claude_account SET label = %s WHERE account_id = %s", (text, found[0])
        )
    connection.commit()


def set_project_label(connection: pg8000.dbapi.Connection, project: str, label: str) -> None:
    """Give a project a label, replacing an earlier one.

    Args:
        connection: A connection that may write the project table.
        project: The project's key or Git remote, one of its folders (exactly as Claude Code wrote
            it; ``ccp-db labels`` lists them), or its name if only one project has that name.
        label: The name to show.

    Raises:
        LabelError: If the project is empty, its name is shared by several projects, it matches
            nothing and is not a folder, or the label is not acceptable.
    """
    key = project.strip()
    if not key:
        raise LabelError("The project is empty")
    text = _checked(label)
    cursor = connection.cursor()
    ids = _project_ids(cursor, key)
    if not ids:
        if "/" not in key and "\\" not in key:
            raise LabelError("No project has that name, key or folder (see: ccp-db labels)")
        cursor.execute(
            "INSERT INTO project (project_key, key_kind, name, folders, label) "
            "VALUES (%s, 'folder', %s, ARRAY[%s], %s) "
            "ON CONFLICT (project_key) DO UPDATE SET label = EXCLUDED.label",
            (normalise_folder(key), folder_name(key), key, text),
        )
    else:
        cursor.execute("UPDATE project SET label = %s WHERE project_id = ANY (%s)", (text, ids))
    connection.commit()


def remove_label(connection: pg8000.dbapi.Connection, kind: str, key: str) -> bool:
    """Remove an account or project label. Returns True if one was removed.

    Raises:
        LabelError: If ``kind`` is not ``account`` or ``project``, or a project name is shared by
            several projects.
    """
    cursor = connection.cursor()
    text = key.strip()
    if kind == "account":
        cursor.execute(
            "UPDATE claude_account SET label = NULL "
            "WHERE (lower(email) = lower(%s) OR account_uuid = %s) AND label IS NOT NULL",
            (text, text),
        )
    elif kind == "project":
        cursor.execute(
            "UPDATE project SET label = NULL WHERE project_id = ANY (%s) AND label IS NOT NULL",
            (_project_ids(cursor, text),),
        )
    else:
        raise LabelError("The kind must be 'account' or 'project'")
    removed = bool(cursor.rowcount > 0)
    connection.commit()
    return removed


def list_labels(connection: pg8000.dbapi.Connection) -> list[Label]:
    """Every Claude account and project, with its label if it has one.

    Accounts and folders seen in a chat but not in the new tables yet are listed too, with an
    empty label, so that an administrator can see what still needs a name. This shows e-mail
    addresses and folder paths: it is for administrators only.
    """
    cursor = connection.cursor()
    cursor.execute(_LIST)
    rows = cursor.fetchall()
    return [Label(str(kind), str(key), str(label), str(name)) for kind, key, label, name in rows]


def _project_ids(cursor: pg8000.dbapi.Cursor, key: str) -> list[int]:
    """The ids of the projects ``key`` names: by key, Git remote or folder, else by name.

    Raises:
        LabelError: If only the name matches and several projects share it.
    """
    cursor.execute(_FIND_PROJECTS, (key, key))
    ids = [int(row[0]) for row in cursor.fetchall()]
    if not ids:
        cursor.execute(
            "SELECT project_id FROM project WHERE git_remote = %s OR name = %s", (key, key)
        )
        ids = [int(row[0]) for row in cursor.fetchall()]
        if len(ids) > 1:
            raise LabelError(
                f"{len(ids)} projects have that name; give the project's key or folder instead"
            )
    return ids


def _checked(label: str) -> str:
    """Return the label without outer spaces, or raise if it is not acceptable."""
    text = label.strip()
    if not _LABEL.fullmatch(text):
        raise LabelError("A label is 1 to 40 letters, digits, spaces, dots, dashes or underscores")
    return text
