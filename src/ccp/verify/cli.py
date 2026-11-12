"""Command-line entry point: ``ccp-verify``.

Examples:
    ccp-verify transcript ~/.claude/projects/my-project/session.jsonl
    ccp-verify transcript ./session-folder --out transcript-report.md
    ccp-verify env
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.verify.environment_inspector import collect
from ccp.verify.transcript_inspector import inspect_files, render_markdown


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser with the ``transcript`` and ``env`` sub-commands."""
    parser = argparse.ArgumentParser(
        prog="ccp-verify", description="Find out what Claude Code writes and where it runs."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    transcript = commands.add_parser("transcript", help="inventory of transcript files")
    transcript.add_argument("paths", nargs="+", type=Path, help="transcript files or folders")
    transcript.add_argument("--out", type=Path, help="write the Markdown report to this file")

    env = commands.add_parser("env", help="describe the machine and surface (JSON)")
    # --out avoids shell redirection, which can write UTF-16 in Windows PowerShell 5.
    env.add_argument("--out", type=Path, help="write the JSON report to this file")
    return parser


def _emit(text: str, out: Path | None) -> None:
    """Print the report, or write it to a file (UTF-8) when ``out`` is given."""
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 2 if an input path does not exist or holds no
        transcript files.
    """
    args = build_parser().parse_args(argv)

    if args.command == "transcript":
        missing = [str(path) for path in args.paths if not path.exists()]
        if missing:
            print(f"Path not found: {', '.join(missing)}", file=sys.stderr)
            return 2
        facts = inspect_files(args.paths)
        if not facts.files:  # a folder without any .jsonl file would otherwise give an empty report
            print("No .jsonl transcript files found.", file=sys.stderr)
            return 2
        _emit(render_markdown(facts), args.out)
    else:
        report_data = collect(os.environ, Path.home(), Path("/.dockerenv"))
        _emit(json.dumps(report_data, indent=2), args.out)
    return 0
