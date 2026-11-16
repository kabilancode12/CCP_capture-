"""Writing the catalog: one chat, or all chats, and the index over them.

Everything written is derived and replaceable: a chat folder is written again from the database and
the raw objects every time, files that no longer belong are deleted, and a chat whose title or
label changed is moved (its old folder is found by the chat id at the end of the folder name and
removed). Chats without a message are not shown, and their folder is removed.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from ccp.catalog import documents
from ccp.catalog.conversation import render_conversation
from ccp.catalog.loader import ChatData, Row, list_catalog_rows, list_chat_ids, load_chat
from ccp.catalog.names import (
    ACCOUNTS_FOLDER,
    CATALOG_ROOT,
    CHAT_FOLDER_PATTERN,
    account_folder,
    chat_folder,
    chat_id8_of_folder,
    project_folder,
    short_id,
    slugify,
)
from ccp.catalog.report import render_report
from ccp.catalog.transcripts import RawStore, read_lines, transcript_streams
from ccp.index.agents import agent_id_of
from ccp.storage import ObjectNotFoundError, StorageError
from ccp.storage.keys import MAIN_STREAM

logger = logging.getLogger("ccp.catalog")

SUMMARY_FILE = "00_summary.json"
REPORT_FILE = "report.md"
INDEX_KEY = f"{CATALOG_ROOT}/index.json"
README_KEY = f"{CATALOG_ROOT}/README.md"
_JSON = "application/json"
_MARKDOWN = "text/markdown; charset=utf-8"


class CatalogError(RuntimeError):
    """Raised when a chat cannot be catalogued (for example, it does not exist)."""


class CatalogStore(RawStore, Protocol):
    """The part of the object store the catalog uses (reads raw data, writes ``catalog/``)."""

    def put(self, key: str, data: bytes, content_type: str = ...) -> None:
        """Store an object, replacing an existing one."""

    def delete(self, key: str) -> None:
        """Delete an object (a missing key is not an error)."""


@dataclass
class CatalogResult:
    """What :func:`catalog_all` did."""

    built: list[str] = field(default_factory=list)  # folders written
    removed_empty: int = 0  # empty chats whose folder was removed
    removed_deleted: int = 0  # folders of chats that no longer exist in the index
    failed: dict[str, str] = field(default_factory=dict)  # chat id -> reason (no content)


class _Listing:
    """The keys under ``catalog/accounts/``, grouped by chat folder, kept up to date as we write."""

    def __init__(self, store: CatalogStore) -> None:
        self.folders: dict[str, set[str]] = defaultdict(set)
        for key in store.list_keys(f"{ACCOUNTS_FOLDER}/"):
            match = CHAT_FOLDER_PATTERN.match(key)
            if match:
                self.folders[match.group(1)].add(key)

    def folders_of(self, session_id: str) -> list[str]:
        """Every chat folder whose name ends with the chat's id8."""
        id8 = short_id(session_id)
        return [f for f in self.folders if chat_id8_of_folder(f.rsplit("/", 1)[-1]) == id8]


def _folder_path(row: Row) -> str:
    """The folder of a chat, from the facts its name is made of."""
    account = account_folder(
        row["account_id"], row["account_uuid"], row["account_label"], row["account_name"]
    )
    project = project_folder(row["project_label"], row["project_name"])
    chat = chat_folder(row["started_at"] or row["last_activity_at"], row["title"], row["chat_id"])
    return f"{ACCOUNTS_FOLDER}/{account}/projects/{project}/chats/{chat}"


def _remove_folder(store: CatalogStore, listing: _Listing, folder: str) -> None:
    for key in sorted(listing.folders.pop(folder, set())):
        store.delete(key)


def _remove_old_folders(
    store: CatalogStore, listing: _Listing, session_id: str, keep: str | None
) -> None:
    """Remove every folder of this chat except ``keep`` (found by the id8; verified by chat id)."""
    for folder in listing.folders_of(session_id):
        if folder == keep:
            continue
        summary_key = f"{folder}/{SUMMARY_FILE}"
        if summary_key in listing.folders[folder]:
            try:
                owner = json.loads(store.get(summary_key)).get("chat_id")
            except (ObjectNotFoundError, ValueError, AttributeError):
                owner = session_id
            if owner != session_id:  # another chat whose id starts the same way
                continue
        _remove_folder(store, listing, folder)


def _agent_file_names(data: ChatData, streams: list[str]) -> dict[str, str]:
    """The file name of each sub-agent stream: ``<agent type>__<agent id8>.json``."""
    types = {a["agent_id"]: a["agent_type"] for a in data.agents}
    names: dict[str, str] = {}
    for stream in streams:
        agent_id = agent_id_of(stream)
        stem = f"{slugify(types.get(agent_id), 40, 'agent')}__{short_id(agent_id)}"
        if f"{stem}.json" in names.values():  # two agents with the same first 8 characters
            stem = f"{slugify(types.get(agent_id), 40, 'agent')}__{slugify(agent_id, 80, 'x')}"
        names[stream] = f"{stem}.json"
    return names


def _build_chat(
    connection: Any,
    store: CatalogStore,
    listing: _Listing,
    session_id: str,
    *,
    light: bool = False,
) -> str | None:
    """Write one chat's folder; returns it, or None for an empty chat (whose folder is removed)."""
    data = load_chat(connection, session_id)
    if data is None:
        raise CatalogError(f"There is no chat {session_id!r}.")
    if data.chat["is_empty"]:
        _remove_old_folders(store, listing, session_id, keep=None)
        return None
    folder = _folder_path(data.chat)
    # A light build (after every turn) leaves the copy of the raw transcript alone when the folder
    # already has one: copying it again at every turn would grow with the chat, and the raw data
    # in sessions/ is the source anyway. A full build (session end, ``build``) refreshes it.
    keep_transcripts = light and f"{folder}/transcript/main.json" in listing.folders.get(
        folder, set()
    )
    streams = transcript_streams(store, session_id)
    agent_streams = sorted(s for s in streams if s != MAIN_STREAM)
    agent_files = _agent_file_names(data, agent_streams)

    files: dict[str, tuple[bytes, str]] = {
        "README.md": (
            documents.build_readme(data, folder.rsplit("/", 1)[-1], len(agent_files)).encode(),
            _MARKDOWN,
        ),
        REPORT_FILE: (render_report(data, folder, len(agent_files)).encode(), _MARKDOWN),
        SUMMARY_FILE: (
            documents.to_json(documents.build_summary(data, folder), sort_keys=True),
            _JSON,
        ),
        "01_conversation.md": (render_conversation(data).encode(), _MARKDOWN),
        "02_sessions.json": (documents.to_json(documents.build_sessions(data)), _JSON),
        "03_decisions.json": (documents.to_json(documents.build_decisions(data)), _JSON),
        "04_tests.json": (documents.to_json(documents.build_tests(data)), _JSON),
        "05_code_changes.json": (documents.to_json(documents.build_code_changes(data)), _JSON),
        "06_corrections.json": (documents.to_json(documents.build_corrections(data)), _JSON),
        "07_subagents_and_workflows.json": (
            documents.to_json(documents.build_subagents(data)),
            _JSON,
        ),
        "08_usage.json": (documents.to_json(documents.build_usage(data)), _JSON),
    }
    if not keep_transcripts:
        files["transcript/main.json"] = (
            documents.to_json(read_lines(store, streams.get(MAIN_STREAM, []))),
            _JSON,
        )
        for stream in agent_streams:
            files[f"transcript/agents/{agent_files[stream]}"] = (
                documents.to_json(read_lines(store, streams[stream])),
                _JSON,
            )

    for name, (content, content_type) in files.items():
        store.put(f"{folder}/{name}", content, content_type)
        listing.folders[folder].add(f"{folder}/{name}")
    wanted = {f"{folder}/{name}" for name in files}
    if keep_transcripts:
        wanted |= {k for k in listing.folders[folder] if f"{folder}/transcript/" in k}
    for key in sorted(listing.folders[folder] - wanted):  # files of an earlier build
        store.delete(key)
        listing.folders[folder].discard(key)
    _remove_old_folders(store, listing, session_id, keep=folder)
    return folder


def _write_index(connection: Any, store: CatalogStore, listing: _Listing) -> None:
    """Write ``catalog/index.json`` and ``catalog/README.md``.

    The index lists the chats that have a folder: accounts, then projects, then chats, each with
    its path in the bucket. It is made from the database and the listing only.
    """
    accounts: dict[str, dict[str, Any]] = {}
    for row in list_catalog_rows(connection):
        folder = _folder_path(row)
        if f"{folder}/{SUMMARY_FILE}" not in listing.folders.get(folder, set()):
            continue  # not built (yet)
        account_path, _, rest = folder.removeprefix(f"{ACCOUNTS_FOLDER}/").partition("/projects/")
        project_name, _, chat_name = rest.partition("/chats/")
        account = accounts.setdefault(
            account_path,
            {
                "account_id": row["account_id"],
                "name": row["account_label"] or row["account_name"],
                "path": f"{ACCOUNTS_FOLDER}/{account_path}",
                "projects": {},
            },
        )
        project = account["projects"].setdefault(
            project_name,
            {
                "project_id": row["project_id"],
                "name": row["project_label"] or row["project_name"],
                "path": f"{ACCOUNTS_FOLDER}/{account_path}/projects/{project_name}",
                "chats": {},
            },
        )
        project["chats"][chat_name] = {
            "chat_id": row["chat_id"],
            "title": row["title"],
            "started_at": row["started_at"],
            "last_activity_at": row["last_activity_at"],
            "path": folder,
        }
    tree = [
        {**account, "projects": [
            {**project, "chats": [project["chats"][name] for name in sorted(project["chats"])]}
            for _, project in sorted(account["projects"].items())
        ]}
        for _, account in sorted(accounts.items())
    ]  # fmt: skip
    store.put(INDEX_KEY, documents.to_json({"accounts": tree}), _JSON)
    store.put(README_KEY, documents.TOP_README.encode(), _MARKDOWN)


def catalog_chat(
    connection: Any, store: CatalogStore, session_id: str, *, light: bool = False
) -> str | None:
    """Write (or update) the catalog folder of one chat, and the index.

    Args:
        connection: A database connection (only read from).
        store: The object store: raw data is read from ``sessions/``, the catalog is written to
            ``catalog/``.
        session_id: The chat id.
        light: Do not copy the raw transcript again if the folder already has a copy (used
            after every turn so that ``report.md`` follows the conversation cheaply).

    Returns:
        The folder of the chat in the bucket (``catalog/accounts/.../chats/<chat>``, no trailing
        slash), or None if the chat is empty (it has no folder; an old one is removed).

    Raises:
        CatalogError: If there is no such chat.
        ccp.storage.StorageError: If the object store fails.
    """
    listing = _Listing(store)
    folder = _build_chat(connection, store, listing, session_id, light=light)
    _write_index(connection, store, listing)
    return folder


def catalog_all(connection: Any, store: CatalogStore) -> CatalogResult:
    """Write every chat's catalog; remove the folders of empty and deleted chats; write the index.

    One chat that cannot be catalogued (its raw data is unreadable) does not stop the others; it
    is reported in the result.
    """
    listing = _Listing(store)
    result = CatalogResult()
    filled, empty = list_chat_ids(connection)
    for session_id in filled:
        try:
            folder = _build_chat(connection, store, listing, session_id)
        except (CatalogError, StorageError) as error:
            logger.warning("catalog of chat %s failed: %s", session_id, type(error).__name__)
            result.failed[session_id] = type(error).__name__
            continue
        if folder:
            result.built.append(folder)
    for session_id in empty:
        if listing.folders_of(session_id):
            _build_chat(connection, store, listing, session_id)
            result.removed_empty += 1
    known = {short_id(session_id) for session_id in (*filled, *empty)}
    for folder in list(listing.folders):
        if chat_id8_of_folder(folder.rsplit("/", 1)[-1]) not in known:
            _remove_folder(store, listing, folder)  # its chat was deleted: nothing to describe
            result.removed_deleted += 1
    _write_index(connection, store, listing)
    return result


def report_of_chat(connection: Any, store: CatalogStore | None, chat_id: str) -> str:
    """The single-file Markdown report of one chat, made now (not read from the bucket).

    Args:
        connection: A database connection (only read from).
        store: The object store (used to count the sub-agent transcripts); None counts none.
        chat_id: The chat id, or the start of it if that is unique.

    Raises:
        CatalogError: If there is no such chat, or the start of the id matches several.
    """
    cursor = connection.cursor()
    cursor.execute(
        "SELECT session_id FROM session WHERE session_id LIKE %s LIMIT 2", (chat_id + "%",)
    )
    found = [row[0] for row in cursor.fetchall()]
    if len(found) != 1:
        raise CatalogError(f"{chat_id!r} matches {len(found)} chats; give more of the id.")
    data = load_chat(connection, found[0])
    if data is None:
        raise CatalogError(f"There is no chat {chat_id!r}.")
    folder = _folder_path(data.chat)
    streams = transcript_streams(store, found[0]) if store is not None else {}
    agents = [s for s in streams if s != MAIN_STREAM]
    return render_report(data, folder, len(agents))
