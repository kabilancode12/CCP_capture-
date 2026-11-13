"""The ``ccp-audit`` command (also ``python -m ccp.audit``).

    ccp-audit history <file>          every recorded AI edit of a file, oldest first
    ccp-audit why <file> <line>       which AI edit wrote a line, and the prompt behind it

``<file>`` is a path relative to the session's folder (``src/app.py``) or an absolute path. Both
commands read only the index (run ``ccp-index run`` first) and print what the recorded edits can
tell: edits made outside Claude Code, and by shell commands, are not recorded (see docs/audit.md).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ccp.audit.provenance import owner_of, replay
from ccp.audit.repository import AuditError, AuditRepository, RecordedEdit
from ccp.settings import SettingsError, load_database_settings

PROMPT_WIDTH = 60
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
        return _why(repository, args.file, args.line)
    except (SettingsError, AuditError) as error:
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
