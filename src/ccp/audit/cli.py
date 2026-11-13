"""The ``ccp-audit`` command (also ``python -m ccp.audit``).

    ccp-audit history <file>          every recorded AI edit of a file, oldest first
    ccp-audit why <file> <line>       which AI edit wrote a line, and the prompt behind it
    ccp-audit link-commits --repo <git>   link commits to the sessions that wrote their lines
    ccp-audit commit <sha>            the sessions a commit was linked to
    ccp-audit session-commits <id>    the commits linked to a session

``<file>`` is a path relative to the session's folder (``src/app.py``) or an absolute path. Both
commands read only the index (run ``ccp-index run`` first) and print what the recorded edits can
tell: edits made outside Claude Code, and by shell commands, are not recorded (see docs/audit.md).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.audit.gitlog import DEFAULT_REFS, GitError, added_lines, list_commits
from ccp.audit.linking import DEFAULT_MIN_CONFIDENCE, link_commit
from ccp.audit.provenance import owner_of, replay
from ccp.audit.repository import AuditError, AuditRepository, RecordedEdit
from ccp.settings import SettingsError, load_database_settings

PROMPT_WIDTH = 60
MIN_SHA_LENGTH = 7
NOTE = (
    "Based on the edits Claude Code recorded. Changes made by a person, by a shell command or "
    "before capture are not visible."
)


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-audit", description="Questions about the index.")
    commands = parser.add_subparsers(dest="command", required=True)

    history = commands.add_parser("history", help="every recorded AI edit of a file")
    history.add_argument("file")
    history.add_argument(
        "--include-abandoned", action="store_true", help="also edits made on rewound branches"
    )

    why = commands.add_parser("why", help="which AI edit wrote a line")
    why.add_argument("file")
    why.add_argument("line", type=int, help="line number (1-based) in the file as it is now")

    link = commands.add_parser("link-commits", help="link commits to the sessions that wrote them")
    link.add_argument("--repo", type=Path, required=True, help="a Git repository (or bare one)")
    link.add_argument(
        "--ref",
        action="append",
        metavar="PATTERN",
        help="reference pattern to read (repeatable; default: the capture branch backups)",
    )
    link.add_argument("--all-refs", action="store_true", help="read every reference")
    link.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_CONFIDENCE,
        help="smallest share of a commit that a session must explain (default %(default)s)",
    )

    commit = commands.add_parser("commit", help="the sessions a commit was linked to")
    commit.add_argument("sha", help="the commit id, or its start (at least 7 characters)")

    session = commands.add_parser("session-commits", help="the commits linked to a session")
    session.add_argument("session", help="the session id, or its start")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Returns:
        0 if the question was answered (even if the answer is "no recorded edit"), 1 on an error.
    """
    args = build_parser().parse_args(argv)
    try:
        repository = AuditRepository(load_database_settings())
        if args.command == "history":
            return _history(repository, args.file, args.include_abandoned)
        if args.command == "link-commits":
            return _link_commits(repository, args)
        if args.command == "commit":
            return _commit(repository, args.sha)
        if args.command == "session-commits":
            return _session_commits(repository, args.session)
        return _why(repository, args.file, args.line)
    except (SettingsError, AuditError, GitError) as error:
        print(error, file=sys.stderr)
        return 1


def _history(repository: AuditRepository, path: str, include_abandoned: bool) -> int:
    edits = repository.file_edits(path, include_abandoned=include_abandoned)
    if not edits:
        print(f"No recorded AI edit of {path}.")
        return 0
    print(f"{len(edits)} recorded edit(s) of {path}, oldest first:\n")
    for number, recorded in enumerate(edits, start=1):
        print(f"{number:>3}. {_describe(recorded)}")
        print(f"     prompt: {_prompt(recorded)}")
    print(f"\n{NOTE}")
    return 0


def _why(repository: AuditRepository, path: str, line: int) -> int:
    if line < 1:
        print("The line number starts at 1.", file=sys.stderr)
        return 1
    edits = repository.file_edits(path)
    if not edits:
        print(f"No recorded AI edit of {path}.")
        return 0
    owner = owner_of(replay(e.edit for e in edits), line)
    if owner is None:
        print(f"No recorded AI edit wrote line {line} of {path}.")
        print("It was written before capture, by a person, or by a shell command.")
    else:
        recorded = next(e for e in edits if e.edit.change_id == owner)
        print(f"Line {line} of {path} was last written by:")
        print(f"  {_describe(recorded)}")
        print(f"  prompt: {_prompt(recorded)}")
        if recorded.user_modified:
            print("  The developer changed the AI's proposal before accepting it.")
    print(f"\n{NOTE}")
    return 0


def _describe(recorded: RecordedEdit) -> str:
    when = recorded.occurred_at.strftime("%Y-%m-%d %H:%M:%S") if recorded.occurred_at else "?"
    flags = " (user-modified)" if recorded.user_modified else ""
    flags += " (rewound)" if recorded.is_abandoned else ""
    return (
        f"{when}  {recorded.tool or '?'}  +{recorded.lines_added}/-{recorded.lines_removed}"
        f"  session {recorded.session_id[:8]}{flags}"
    )


def _prompt(recorded: RecordedEdit) -> str:
    text = " ".join((recorded.prompt or "").split())
    if not text:
        return "(not found)"
    return text if len(text) <= PROMPT_WIDTH else text[: PROMPT_WIDTH - 3] + "..."


def _link_commits(repository: AuditRepository, args: argparse.Namespace) -> int:
    if not 0 < args.min_confidence <= 1:
        print("--min-confidence must be above 0 and at most 1.", file=sys.stderr)
        return 1
    refs = () if args.all_refs else tuple(args.ref or DEFAULT_REFS)
    commits = list_commits(args.repo, refs)
    ai_lines = repository.ai_added_lines()
    linked = 0
    for commit in commits:
        links = link_commit(
            added_lines(args.repo, commit.sha),
            commit.committed_at,
            ai_lines,
            min_confidence=args.min_confidence,
        )
        repository.save_links(commit.sha, commit.committed_at, links)
        if links:
            linked += 1
            shares = ", ".join(f"{link.session_id[:8]} {link.confidence:.0%}" for link in links)
            print(f"{commit.sha[:10]}  {shares}")
    print(f"{linked} of {len(commits)} commit(s) linked to a session.")
    return 0


def _commit(repository: AuditRepository, sha: str) -> int:
    if len(sha) < MIN_SHA_LENGTH:
        print(f"Give at least {MIN_SHA_LENGTH} characters of the commit id.", file=sys.stderr)
        return 1
    links = repository.links_of_commit(sha)
    if not links:
        print(f"No session is linked to {sha}. (Run ccp-audit link-commits first; commits made")
        print("by hand, or before capture, have no link.)")
        return 0
    for link in links:
        started = (
            link.session_started_at.strftime("%Y-%m-%d %H:%M") if link.session_started_at else "?"
        )
        print(
            f"{link.commit_sha[:10]}  session {link.session_id}  {link.confidence:.0%} "
            f"({link.matched_lines} of {link.total_lines} lines)  started {started}"
            f"  branch {link.branch or '?'}"
        )
    print("\nA link is a likelihood: the share of the commit's added lines that the session's")
    print("recorded edits also added. Several sessions can each explain part of one commit.")
    return 0


def _session_commits(repository: AuditRepository, session: str) -> int:
    rows = repository.commits_of_session(session)
    if not rows:
        print(f"No commit is linked to a session starting {session}.")
        return 0
    for sha, confidence, matched, total, when in rows:
        stamp = when.strftime("%Y-%m-%d %H:%M") if when else "?"
        print(f"{str(sha)[:10]}  {stamp}  {confidence:.0%} ({matched} of {total} lines)")
    return 0
