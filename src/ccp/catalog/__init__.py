"""A readable catalog of the captured chats, kept in the object store next to the raw data.

The raw objects (``sessions/<id>/<stream>/<offset>.jsonl``) are exact but opaque: ids for names,
one JSON line per entry, split into chunks. The catalog is a **derived, rebuildable** view of the
same chats under ``catalog/``: folders named after the Claude account, the project and the chat,
a readable conversation, and the facts the index knows (tests, code changes, corrections ...).
Nothing in ``sessions/`` is ever touched, and deleting ``catalog/`` loses nothing.

Modules:
    names: slugs and the folder layout.
    loader: reads one chat's facts from the database.
    conversation: turns the events into a readable Markdown conversation.
    documents: builds the JSON files and the README of a chat.
    transcripts: reads the raw lines of a chat back from the object store.
    builder: writes a chat (or all chats) into the catalog, removes stale folders, writes the index.
    cli: the ``ccp-catalog`` command.
"""

from ccp.catalog.builder import CatalogError, catalog_all, catalog_chat
from ccp.catalog.plugin import register

__all__ = ["CatalogError", "catalog_all", "catalog_chat", "register"]
