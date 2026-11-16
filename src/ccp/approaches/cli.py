"""The ``ccp-approaches`` command (also ``python -m ccp.approaches``).

ccp-approaches refresh                      work out where each decision's code is, what replaced it
ccp-approaches search <words> [--removed]   find approaches (every word must appear)
ccp-approaches show <id>                    one approach: why, options, code, what became of it
ccp-approaches compare <id> <id> [--repo R] two approaches side by side (and their code difference)
ccp-approaches restore <id> --repo R --into DIR --by NAME --purpose TEXT
bring an approach's files back, and record the reuse
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.approaches import library
from ccp.approaches.lineage import refresh_lineage
from ccp.audit.gitlog import GitError
from ccp.db.connection import connect
from ccp.settings import SettingsError, load_database_settings

STATE = {"active": "ACTIVE", "superseded": "REPLACED", "removed": "REMOVED"}


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-approaches", description="The approach library.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("refresh", help="recompute where code is and what removed it")
    search = commands.add_parser("search", help="find approaches")
    search.add_argument("words", nargs="+")
    search.add_argument("--removed", action="store_true", help="only replaced or removed ones")
    show = commands.add_parser("show", help="one approach in full")
    show.add_argument("id", type=int)
    compare = commands.add_parser("compare", help="two approaches side by side")
    compare.add_argument("first", type=int)
    compare.add_argument("second", type=int)
    compare.add_argument("--repo", type=Path, help="a clone of the capture project (for the diff)")
    restore = commands.add_parser("restore", help="bring an approach's files back")
    restore.add_argument("id", type=int)
    restore.add_argument("--repo", type=Path, required=True, help="a clone of the capture project")
    restore.add_argument("--into", type=Path, required=True, help="folder to write the files into")
    restore.add_argument("--by", required=True, help="who restores it")
    restore.add_argument("--purpose", required=True, help="what it is used for")
    restore.add_argument("--force", action="store_true", help="overwrite existing files")
    return parser


def line(item: library.Approach) -> str:
    """One search result line."""
    why = f"  (replaced by: {item.removed_by})" if item.removed_by else ""
    reused = f"  reused {item.reuses}x" if item.reuses else ""
    return (
        f"#{item.decision_id:<5} {STATE.get(item.lifecycle, item.lifecycle):<8} {item.title or '?'}"
        f"  [{item.project or '?'}, {item.account or '?'}, {item.decided_by or '?'}]{why}{reused}"
    )


def describe(item: library.Approach) -> list[str]:
    """An approach in full, as plain lines."""
    out = [
        f"#{item.decision_id}  {item.title}  [{STATE.get(item.lifecycle, item.lifecycle)}]",
        f"  project {item.project or '?'}, chat {item.session_id[:8]} ({item.chat_title or '?'}), "
        f"account {item.account or '?'}",
        f"  decided by: {item.decided_by or '?'}   written by model: {item.model or '?'}   "
        f"review: {item.status}",
        f"  goal:   {item.goal or '-'}",
        "  options:",
        *[f"    - {o}" for o in item.options or []],
        f"  chosen: {item.chosen or '-'}",
        f"  why:    {item.reason or '-'}",
        f"  result: {item.result or '-'}",
        f"  tests:  {item.tests_summary or '-'}",
        "  files:  " + (", ".join(p for p, _ in item.file_paths()) or "-"),
        f"  code:   capture commit {item.code_sha[:10] if item.code_sha else 'none recorded'}",
    ]
    if item.lifecycle != "active":
        when = f"{item.removed_at:%Y-%m-%d %H:%M}" if item.removed_at else "?"
        out.append(
            f"  removed {when} in commit {(item.removed_in_sha or '?')[:10]}"
            + (f" by decision #{item.removed_by_id}: {item.removed_by}" if item.removed_by else "")
        )
    if item.supersedes:
        out.append(f"  replaced decision #{item.supersedes_id}: {item.supersedes}")
    for note in item.thinking or []:
        if isinstance(note, dict) and note.get("text"):
            out.append(f"  thinking: {' '.join(str(note['text']).split())[:300]}")
    for use in item.reuse_log:
        out.append(f"  reused by {use['reused_by']} for: {use['purpose']} -> {use['target']}")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command; 0 on success, 1 on an error."""
    args = build_parser().parse_args(argv)
    try:
        connection = connect(load_database_settings())
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        code = _run(connection, args)
        connection.commit()
        return code
    except (library.RestoreError, GitError) as error:
        connection.rollback()
        print(error, file=sys.stderr)
        return 1
    finally:
        connection.close()


def _run(connection: object, args: argparse.Namespace) -> int:
    if args.command == "refresh":
        print(f"{refresh_lineage(connection)} decision(s) have code on the capture chain.")
        return 0
    if args.command == "search":
        found = library.search(connection, " ".join(args.words), only_removed=args.removed)
        print("\n".join(map(line, found)) if found else "No approach matches.")
        return 0
    if args.command == "restore":
        item = library.get(connection, args.id)
        if item is None:
            print(f"There is no decision #{args.id}.", file=sys.stderr)
            return 1
        files = library.restore(
            connection,
            args.repo,
            item,
            args.into,
            by=args.by,
            purpose=args.purpose,
            force=args.force,
        )
        print(
            f"Restored {len(files)} file(s) of #{item.decision_id} {item.title} into {args.into}:"
        )
        print("\n".join(f"  {path}" for path in files))
        return 0
    first = library.get(connection, args.id if args.command == "show" else args.first)
    if first is None:
        print("There is no such decision.", file=sys.stderr)
        return 1
    print("\n".join(describe(first)))
    if args.command == "compare":
        second = library.get(connection, args.second)
        if second is None:
            print("There is no such decision.", file=sys.stderr)
            return 1
        print("\n" + "-" * 60 + "\n" + "\n".join(describe(second)))
        if args.repo:
            print(
                "\n".join(
                    [
                        "",
                        "code difference (first -> second):",
                        *library.diff(args.repo, first, second),
                    ]
                )
            )
    return 0
