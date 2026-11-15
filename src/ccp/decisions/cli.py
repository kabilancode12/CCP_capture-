"""The decisions command (``python -m ccp.decisions``).

    python -m ccp.decisions run --chat ID          extract and review the decisions of one chat
    python -m ccp.decisions run --pending          every chat without up-to-date decisions
    python -m ccp.decisions run ... --force        also chats whose decisions are up to date
    python -m ccp.decisions run ... --dry-run      only print the size of each digest
    python -m ccp.decisions list --chat ID         the decisions of a chat
    python -m ccp.decisions review ID approve|reject --by NAME [--note TEXT]

``run`` calls a model (the ``claude`` command, or the Anthropic API if configured): see
docs/decisions.md. It prints counts, never the text of a chat.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import pg8000.dbapi

from ccp.db.connection import connect
from ccp.decisions import runner
from ccp.decisions.provider import Provider, choose_provider
from ccp.decisions.repository import (
    DecisionError,
    find_chats,
    list_decisions,
    pending_chats,
    set_decision_status,
)
from ccp.decisions.settings import DecisionSettings, load_decision_settings
from ccp.settings import SettingsError, load_database_settings

_VERDICTS = {"approve": "approved", "reject": "rejected"}


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m ccp.decisions", description="Key decisions of a chat."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="extract and review decisions")
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--chat", help="the chat id (or the start of it)")
    which.add_argument("--pending", action="store_true", help="every chat that needs decisions")
    run.add_argument("--force", action="store_true", help="also when the decisions are up to date")
    run.add_argument("--dry-run", action="store_true", help="print the digest size only")

    show = commands.add_parser("list", help="the decisions of a chat")
    show.add_argument("--chat", required=True, help="the chat id (or the start of it)")

    review = commands.add_parser("review", help="a person approves or rejects a decision")
    review.add_argument("decision_id", type=int)
    review.add_argument("verdict", choices=sorted(_VERDICTS))
    review.add_argument("--by", required=True, help="who reviewed (name)")
    review.add_argument("--note", help="an optional comment")
    return parser


def main(argv: Sequence[str] | None = None, provider: Provider | None = None) -> int:
    """Run the command.

    Args:
        argv: The arguments (default: ``sys.argv``).
        provider: The model to ask for ``run`` (default: chosen from the environment). Tests pass
            a fake one.

    Returns:
        0 on success, 1 if anything failed.
    """
    args = build_parser().parse_args(argv)
    try:
        connection = connect(load_database_settings())
    except (SettingsError, pg8000.dbapi.Error, OSError) as error:
        print(f"cannot reach the database: {type(error).__name__}", file=sys.stderr)
        return 1
    try:
        if args.command == "run":
            return _run(connection, args, provider)
        if args.command == "list":
            return _list(connection, args.chat)
        return _review(connection, args)
    except (SettingsError, DecisionError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        connection.close()


def _resolve(connection: pg8000.dbapi.Connection, text: str) -> str:
    found = find_chats(connection, text)
    if not found:
        raise DecisionError(f"no chat {text!r}")
    if len(found) > 1:
        raise DecisionError(f"{text!r} matches {len(found)} chats; give more of the id")
    return found[0]


def _run(
    connection: pg8000.dbapi.Connection, args: argparse.Namespace, provider: Provider | None
) -> int:
    settings = load_decision_settings()
    if args.pending:
        chats = pending_chats(connection, args.force)
    else:
        chats = [_resolve(connection, args.chat)]
    if not chats:
        print("No chat needs decisions.")
        return 0
    use = None if args.dry_run else (provider or choose_provider(settings))
    failed = 0
    for session_id in chats:
        try:
            result = runner.process_chat(
                connection, use, settings, session_id, force=args.force, dry_run=args.dry_run
            )
        except DecisionError as error:
            failed += 1
            print(f"{session_id[:8]}  failed: {error}", file=sys.stderr)
            continue
        print(_describe(result, settings))
    return 1 if failed else 0


def _describe(result: runner.ChatResult, settings: DecisionSettings) -> str:
    name = result.session_id[:8]
    if result.outcome == runner.DONE:
        return (
            f"{name}  {result.decisions} decision(s): {result.approved} approved by AI, "
            f"{result.needs_review} need review; {result.corrections} developer correction(s); "
            f"digest {result.digest_chars} characters ({settings.model})"
        )
    if result.outcome == runner.DRY_RUN:
        return f"{name}  digest {result.digest_chars} characters (limit {settings.max_chars})"
    if result.outcome == runner.NO_DECISIONS:
        return f"{name}  the model returned no valid decision; nothing changed"
    if result.outcome == runner.UP_TO_DATE:
        return f"{name}  decisions are up to date (use --force to redo)"
    if result.outcome == runner.EMPTY:
        return f"{name}  empty chat, skipped"
    return f"{name}  no such chat"


def _list(connection: pg8000.dbapi.Connection, text: str) -> int:
    session_id = _resolve(connection, text)
    rows = list_decisions(connection, session_id)
    if not rows:
        print(f"Chat {session_id[:8]} has no decisions.")
        return 0
    for row in rows:
        who = f"{row.reviewed_by_kind or '?'}: {row.reviewer or '?'}"
        print(
            f"#{row.decision_id}  {row.seq}. [{row.status}] {row.title or '(untitled)'}  "
            f"(reviewed by {who}; {row.evidence_count} evidence line(s), "
            f"{row.history_count} review(s))"
        )
        if row.review_note:
            print(f"      note: {row.review_note}")
    return 0


def _review(connection: pg8000.dbapi.Connection, args: argparse.Namespace) -> int:
    set_decision_status(connection, args.decision_id, _VERDICTS[args.verdict], args.by, args.note)
    print(f"Decision {args.decision_id} is now {_VERDICTS[args.verdict]} (by {args.by.strip()}).")
    return 0
