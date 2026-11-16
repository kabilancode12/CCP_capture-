"""The ``ccp-catalog`` command (also ``python -m ccp.catalog``).

    ccp-catalog build --chat <id>     write the catalog folder of one chat
    ccp-catalog build --all           write every chat, remove empty chats, write the index
    ccp-catalog show [--files]        print the catalog as a tree (``--files`` lists each file)

``build`` reads the index in PostgreSQL and the raw objects, and writes only under ``catalog/``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from ccp.catalog.builder import CatalogError, catalog_all, catalog_chat
from ccp.catalog.names import CATALOG_ROOT
from ccp.db.connection import connect
from ccp.settings import SettingsError, load_database_settings, load_storage_settings
from ccp.storage import StorageError, connect_store

_NAMELESS_LEVELS = ("accounts", "projects", "sessions")  # folders that only organise; not printed


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-catalog", description="The readable chat catalog.")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="write the catalog")
    which = build.add_mutually_exclusive_group(required=True)
    which.add_argument("--chat", metavar="ID", help="one chat")
    which.add_argument("--all", action="store_true", help="every chat")
    export = commands.add_parser("export", help="write one chat's report as a single Markdown file")
    export.add_argument("--chat", metavar="ID", required=True, help="the chat id")
    export.add_argument(
        "--out", metavar="FILE", help="file to write (default: print to the screen)"
    )
    show = commands.add_parser("show", help="print the catalog as a tree")
    show.add_argument("--files", action="store_true", help="also list the files of each chat")
    return parser


def tree_lines(keys: Sequence[str], with_files: bool) -> list[str]:
    """The catalog keys as an indented tree: account, project, chat (and files if asked)."""
    lines = [f"{CATALOG_ROOT}/"]
    seen: set[tuple[str, ...]] = set()
    # The files of the catalog itself first, then the chats.
    for key in sorted(keys, key=lambda k: (k.startswith(f"{CATALOG_ROOT}/accounts/"), k)):
        parts = key.split("/")[1:]  # without "catalog"
        shown = [p for i, p in enumerate(parts) if not (p in _NAMELESS_LEVELS and i % 2 == 0)]
        in_chats = parts[0] == "accounts"
        depth_limit = 3 if in_chats and not with_files else len(shown)  # account, project, chat
        for depth in range(min(len(shown), depth_limit)):
            path = tuple(shown[: depth + 1])
            if path not in seen:
                seen.add(path)
                lines.append("  " * (depth + 1) + shown[depth])
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Returns:
        0 on success, 1 on an error (also when some chats of ``build --all`` failed).
    """
    args = build_parser().parse_args(argv)
    try:
        store = connect_store(load_storage_settings())
        if args.command == "show":
            keys = store.list_keys(f"{CATALOG_ROOT}/")
            print("\n".join(tree_lines(keys, args.files)) if keys else "The catalog is empty.")
            return 0
        connection = connect(load_database_settings())
        try:
            if args.command == "export":
                return _export(connection, store, args)
            return _build(connection, store, args)
        finally:
            connection.close()
    except (SettingsError, StorageError, CatalogError, OSError) as error:
        print(error, file=sys.stderr)
        return 1


def _export(connection: Any, store: Any, args: argparse.Namespace) -> int:
    """Print or write the single-file report of one chat (the chat id may be shortened)."""
    from ccp.catalog.builder import report_of_chat

    text = report_of_chat(connection, store, args.chat)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(f"Wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _build(connection: Any, store: Any, args: argparse.Namespace) -> int:
    if args.all:
        result = catalog_all(connection, store)
        print(
            f"{len(result.built)} chat(s) written, {result.removed_empty} empty chat(s) removed, "
            f"{result.removed_deleted} deleted chat(s) removed."
        )
        for session_id, reason in result.failed.items():
            print(f"failed: {session_id} ({reason})", file=sys.stderr)
        return 1 if result.failed else 0
    folder = catalog_chat(connection, store, args.chat)
    print(f"Written: {folder}" if folder else "The chat is empty: nothing written.")
    return 0
