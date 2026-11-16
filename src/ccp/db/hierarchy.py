r"""Which Claude account and which project a chat belongs to (migration 016).

A chat (a row of ``session``) is linked to:

* its **Claude account** (table ``claude_account``): found by the account uuid of the environment
  record. Accounts seen before uuids were collected are known by their e-mail address only; such a
  row takes over the uuid when it first arrives, so its label is kept. It is found by its e-mail
  address, or, if the record has no e-mail address or another one, as the chat's current account.
* its **project** (table ``project``): the normalised Git ``origin`` remote of the chat's folder
  (``github.com/acme/billing``) if it has one, otherwise the folder itself, keyed by
  ``<host name>:<folder>`` (the folder alone if the host is unknown). Every folder a project was
  seen in is added to ``project.folders``. A Windows folder is keyed with a lower-case drive letter
  and backslashes (:func:`normalise_folder`), because Claude Code writes ``C:\work`` in one place
  and ``c:\work`` in another.

A folder can have a project of its own before the chat's project: an administrator may label a
folder in advance (``ccp-db label project <folder>``; keyed by the folder alone), and every folder
was its own project (``<host name>:<folder>``) until it got a Git remote. Such a *folder project* is
merged into the chat's project, so that one repository is one project and no label is lost:

* the chat's project takes the folder project's label if it has none of its own (if both have one,
  the chat's project keeps its own);
* the folder project's chats, folders and first-seen time move to the chat's project;
* the folder project is deleted.

So the first chat with a Git remote in a folder brings that folder's earlier chats and label along.

:func:`link_chat` does both from the stored environment record. The ingestion API calls it each time
an environment record arrives; it is safe to call again at any time (the indexer may call it once a
transcript names the folder of a chat whose client did not send it).

Migration 016 filled the tables once for the chats captured before, with the same key rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pg8000.dbapi

from ccp.capture.gitinfo import strip_credentials

_SCP_LIKE = re.compile(r"(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>[^/].*)")
_WINDOWS_FOLDER = re.compile(r"[A-Za-z]:[\\/]")

_READ_ENVIRONMENT = """
    SELECT env.account_uuid, env.account_email, env.account_name, env.full_name,
           env.organisation_uuid, env.organisation_name, env.organisation_role,
           env.hostname, COALESCE(env.folder, f.folder), env.git_remote,
           env.git_user_name, env.git_user_email
      FROM session_env env
      LEFT JOIN v_session_folder f USING (session_id)
     WHERE env.session_id = %s
"""

# An account known only by its e-mail address takes over the uuid when it first arrives.
_ADOPT_EMAIL_ONLY_ACCOUNT = """
    UPDATE claude_account SET account_uuid = %s
     WHERE account_uuid IS NULL AND lower(email) = lower(%s)
       AND NOT EXISTS (SELECT 1 FROM claude_account WHERE account_uuid = %s)
"""

# Insert an account, or update it: known fields replace the stored ones, unknown (NULL) ones leave
# them. The first one finds the account by uuid, the second (no uuid known) by e-mail address.
_UPSERT_ACCOUNT_BY_UUID = """
    INSERT INTO claude_account (account_uuid, email, full_name, display_name, organisation_uuid,
                                organisation_name, organisation_role)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (account_uuid) DO UPDATE SET
        email = COALESCE(EXCLUDED.email, claude_account.email),
        full_name = COALESCE(EXCLUDED.full_name, claude_account.full_name),
        display_name = COALESCE(EXCLUDED.display_name, claude_account.display_name),
        organisation_uuid = COALESCE(EXCLUDED.organisation_uuid, claude_account.organisation_uuid),
        organisation_name = COALESCE(EXCLUDED.organisation_name, claude_account.organisation_name),
        organisation_role = COALESCE(EXCLUDED.organisation_role, claude_account.organisation_role),
        last_seen_at = now()
    RETURNING account_id
"""

_UPSERT_ACCOUNT_BY_EMAIL = """
    INSERT INTO claude_account (account_uuid, email, full_name, display_name, organisation_uuid,
                                organisation_name, organisation_role)
    VALUES (NULL, lower(%s), %s, %s, %s, %s, %s)
    ON CONFLICT (lower(email)) WHERE account_uuid IS NULL DO UPDATE SET
        full_name = COALESCE(EXCLUDED.full_name, claude_account.full_name),
        display_name = COALESCE(EXCLUDED.display_name, claude_account.display_name),
        organisation_uuid = COALESCE(EXCLUDED.organisation_uuid, claude_account.organisation_uuid),
        organisation_name = COALESCE(EXCLUDED.organisation_name, claude_account.organisation_name),
        organisation_role = COALESCE(EXCLUDED.organisation_role, claude_account.organisation_role),
        last_seen_at = now()
    RETURNING account_id
"""

# A known e-mail address whose account already has a uuid: use that account.
_ACCOUNT_BY_EMAIL = """
    SELECT account_id FROM claude_account
     WHERE lower(email) = lower(%s)
     ORDER BY account_uuid IS NULL, account_id LIMIT 1
"""

_UPSERT_PROJECT = """
    INSERT INTO project (project_key, key_kind, name, git_remote, folders)
    VALUES (%s, %s, %s, %s, CASE WHEN %s::text IS NULL THEN '{}'::text[] ELSE ARRAY[%s::text] END)
    ON CONFLICT (project_key) DO UPDATE SET
        git_remote = COALESCE(EXCLUDED.git_remote, project.git_remote),
        folders = CASE WHEN EXCLUDED.folders <@ project.folders THEN project.folders
                       ELSE project.folders || EXCLUDED.folders END,
        last_seen_at = now()
    RETURNING project_id
"""

# An account known only by its e-mail address that the chat is linked to already takes over the
# uuid (the record has no e-mail address, or another one than that row).
_ADOPT_CHAT_ACCOUNT = """
    UPDATE claude_account SET account_uuid = %s
     WHERE account_uuid IS NULL
       AND account_id = (SELECT account_id FROM session WHERE session_id = %s)
       AND NOT EXISTS (SELECT 1 FROM claude_account WHERE account_uuid = %s)
"""

# The folder projects (see above) of the chat's folder, other than the chat's project itself.
_FIND_FOLDER_PROJECTS = """
    SELECT project_id FROM project
     WHERE project_key = ANY (%s) AND key_kind = 'folder' AND project_id <> %s
     ORDER BY project_id
     FOR UPDATE
"""

# Merge a folder project (second id) into the chat's project (first id).
_MERGE_PROJECT = """
    UPDATE project SET
        label = COALESCE(project.label, old.label),
        folders = project.folders
                  || ARRAY(SELECT f FROM unnest(old.folders) f WHERE f <> ALL (project.folders)),
        first_seen_at = LEAST(project.first_seen_at, old.first_seen_at)
      FROM project old
     WHERE project.project_id = %s AND old.project_id = %s
"""
_PROJECT_OF_FOLDER = """
    SELECT project_id FROM project WHERE %s = ANY (folders)
     ORDER BY key_kind = 'git_remote' DESC, project_id LIMIT 1
"""
# A Git project that already has a chat of this machine in this folder: a chat in the same folder
# that records no remote (it was made before the remote was set) belongs to that project too.
_GIT_PROJECT_OF_FOLDER = """
    SELECT p.project_id FROM project p
     WHERE p.key_kind = 'git_remote' AND %s = ANY (p.folders)
       AND EXISTS (SELECT 1 FROM session s JOIN session_env e ON e.session_id = s.session_id
                    WHERE s.project_id = p.project_id AND e.folder = %s
                      AND lower(btrim(e.hostname)) = %s)
     ORDER BY p.project_id LIMIT 1
"""
_MOVE_CHATS = "UPDATE session SET project_id = %s WHERE project_id = %s"
_DELETE_PROJECT = "DELETE FROM project WHERE project_id = %s"

_LINK_CHAT = """
    UPDATE session SET
        account_id = COALESCE(%s, account_id),
        project_id = COALESCE(%s, project_id),
        git_user_name = COALESCE(%s, git_user_name),
        git_user_email = COALESCE(%s, git_user_email)
     WHERE session_id = %s
"""


@dataclass(frozen=True)
class ProjectKey:
    """How a project is identified.

    Attributes:
        key: The unique key (``project.project_key``).
        kind: ``git_remote`` or ``folder``.
        name: A short name: the last part of the remote or folder.
    """

    key: str
    kind: str
    name: str


def normalise_git_remote(url: str) -> str | None:
    """Return a Git remote as ``host/path`` in lower case, without credentials, port or ``.git``.

    ``https://user:token@GitHub.com/Acme/App.git``, ``ssh://git@github.com:22/acme/app`` and
    ``git@github.com:acme/app.git`` all give ``github.com/acme/app``. A local path (``/srv/app.git``
    or ``file:///srv/app.git``) gives ``local/srv/app``.

    Returns:
        The normalised remote, or ``None`` if nothing usable is left.
    """
    text = url.strip().replace("\\", "/")
    if not text:
        return None
    match = _SCP_LIKE.fullmatch(text)
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:  # for example a malformed IPv6 address
            return None
        host, path = (parts.hostname or "local"), parts.path
    elif match and len(match["host"]) > 1:  # "C:/repos/app.git" is a Windows path, not a host
        host, path = match["host"], match["path"]
    else:
        host, path = "local", text
    path = path.strip("/").removesuffix(".git").strip("/")
    if not path:
        return None
    return f"{host}/{path}".lower()


def normalise_folder(folder: str) -> str:
    r"""Return a folder as it is used in a project key.

    A Windows folder (``C:\work/app``) gets a lower-case drive letter and backslashes only
    (``c:\work\app``); Windows treats both spellings as the same folder. Any other folder is
    returned unchanged.
    """
    if _WINDOWS_FOLDER.match(folder):
        return folder[0].lower() + folder[1:].replace("/", "\\")
    return folder


def folder_name(folder: str) -> str:
    """Return the last part of a folder (``/home/dev/ccp_V2`` gives ``ccp_V2``), or the folder."""
    name = re.sub(r"^.*[/\\]", "", folder.rstrip("/\\")) or folder
    return re.sub(r"\s+", " ", name).strip() or name


def project_key(
    git_remote: str | None, hostname: str | None, folder: str | None
) -> ProjectKey | None:
    """Return the key of a chat's project: its Git remote if usable, else its folder.

    Args:
        git_remote: The ``origin`` remote as the client reported it.
        hostname: The machine's name (part of a folder key: the same path on two machines is two
            projects).
        folder: The chat's working folder.

    Returns:
        The key, or ``None`` if neither a remote nor a folder is known.
    """
    remote = normalise_git_remote(git_remote) if git_remote else None
    if remote is not None:
        return ProjectKey(remote, "git_remote", remote.rsplit("/", 1)[-1])
    if not folder:
        return None
    return ProjectKey(folder_keys(hostname, folder)[0], "folder", folder_name(folder))


def folder_keys(hostname: str | None, folder: str) -> list[str]:
    """Return the keys a folder project of this folder can have, the usual one first.

    ``<host name>:<folder>`` (host name trimmed and in lower case), then the folder alone (the key
    of a folder labelled before any chat ran in it). Only the folder alone if the host is unknown.
    """
    host = (hostname or "").strip().lower()
    key = normalise_folder(folder)
    return [f"{host}:{key}", key] if host else [key]


def link_chat(
    cursor: pg8000.dbapi.Cursor,
    session_id: str,
    *,
    owner_account_uuid: str | None = None,
    owner_organisation_uuid: str | None = None,
) -> None:
    """Link a chat to its Claude account and project, from its stored environment record.

    Creates or updates the account and project rows as needed. Runs in the caller's transaction;
    the caller commits.

    A chat without an environment record (captured by an older client) is linked from what its
    transcript says, which only the indexer knows: ``owner_account_uuid`` (from the transcript's
    ``session-owner`` lines) and the folder of its first main-transcript line
    (``v_session_folder``).
    The record's own account uuid comes first when there is one. Does nothing if the chat has
    neither a record nor anything from the transcript.

    Args:
        cursor: A cursor of a connection that may write the tables.
        session_id: The chat's id.
        owner_account_uuid: The account uuid the transcript names, if the indexer found one.
        owner_organisation_uuid: The organisation uuid next to it.
    """
    cursor.execute(_READ_ENVIRONMENT, (session_id,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute("SELECT folder FROM v_session_folder WHERE session_id = %s", (session_id,))
        folder_row = cursor.fetchone()
        folder = folder_row[0] if folder_row is not None else None
        if folder is None and owner_account_uuid is None:
            return
        row = (None, None, None, None, None, None, None, None, folder, None, None, None)
    account_uuid = row[0] or owner_account_uuid
    email, display_name, full_name = row[1], row[2], row[3]
    organisation_uuid = row[4] or owner_organisation_uuid
    account = (email, full_name, display_name, organisation_uuid, *row[5:7])  # + name and role
    hostname, folder, git_remote, git_user_name, git_user_email = row[7:12]
    account_id = _account_id(cursor, session_id, account_uuid, account)
    key = project_key(git_remote, hostname, folder)
    project_id = None
    if row[7] is None and git_remote is None and folder:
        # Only the folder is known (no record, or no host name): a project that already holds the
        # folder is the chat's project, a Git one first, as the view ``chat`` matches it.
        cursor.execute(_PROJECT_OF_FOLDER, (folder,))
        project_id = _first_value(cursor)
        key = None if project_id is not None else key
    if key is not None and key.kind == "folder" and hostname and folder:
        # No remote recorded, but this machine's chats in this folder already have a Git project
        # (the remote was set later): the chat joins it instead of making a folder project again.
        cursor.execute(_GIT_PROJECT_OF_FOLDER, (folder, folder, hostname.strip().lower()))
        project_id = _first_value(cursor)
        if project_id is not None:
            _merge_folder_projects(cursor, project_id, folder_keys(hostname, folder))
            key = None
    if key is not None:
        remote = strip_credentials(git_remote) if key.kind == "git_remote" and git_remote else None
        cursor.execute(_UPSERT_PROJECT, (key.key, key.kind, key.name, remote, folder, folder))
        project_id = _first_value(cursor)
        if folder:
            _merge_folder_projects(cursor, project_id, folder_keys(hostname, folder))
    cursor.execute(_LINK_CHAT, (account_id, project_id, git_user_name, git_user_email, session_id))


def _merge_folder_projects(cursor: pg8000.dbapi.Cursor, project_id: Any, keys: list[str]) -> None:
    """Merge the folder projects of the chat's folder into the chat's project (see above).

    Args:
        cursor: The caller's cursor.
        project_id: The chat's project.
        keys: The keys a folder project of the chat's folder can have (:func:`folder_keys`).
    """
    cursor.execute(_FIND_FOLDER_PROJECTS, (keys, project_id))
    for (old_id,) in cursor.fetchall():
        cursor.execute(_MERGE_PROJECT, (project_id, old_id))
        cursor.execute(_MOVE_CHATS, (project_id, old_id))
        cursor.execute(_DELETE_PROJECT, (old_id,))


def _account_id(
    cursor: pg8000.dbapi.Cursor,
    session_id: str,
    account_uuid: str | None,
    fields: tuple[Any, ...],
) -> Any:
    """Find or create the account and return its id (``None`` if neither uuid nor e-mail is known).

    Args:
        cursor: The caller's cursor.
        session_id: The chat's id (its current account may take over the uuid, see above).
        account_uuid: The account uuid, if the client sent it.
        fields: E-mail, full name, display name, organisation uuid, name and role.
    """
    email = fields[0]
    if account_uuid:
        if email:
            cursor.execute(_ADOPT_EMAIL_ONLY_ACCOUNT, (account_uuid, email, account_uuid))
        cursor.execute(_ADOPT_CHAT_ACCOUNT, (account_uuid, session_id, account_uuid))
        cursor.execute(_UPSERT_ACCOUNT_BY_UUID, (account_uuid, *fields))
        return _first_value(cursor)
    if not email:
        return None
    cursor.execute(_ACCOUNT_BY_EMAIL, (email,))
    found = cursor.fetchone()
    if found is not None:
        return found[0]
    cursor.execute(_UPSERT_ACCOUNT_BY_EMAIL, fields)
    return _first_value(cursor)


def _first_value(cursor: pg8000.dbapi.Cursor) -> Any:
    """Return the first column of the row the last statement returned."""
    row = cursor.fetchone()
    return row[0] if row is not None else None
