"""Command-line entry point: ``ccp-verify``.

Examples:
    ccp-verify transcript ~/.claude/projects/my-project/session.jsonl
    ccp-verify transcript ./session-folder --out transcript-report.md
    ccp-verify env --out env.json
    ccp-verify probe-settings          # settings that register the hook probe
    ccp-verify probe-report            # summary of what the probe recorded
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.verify import hook_probe
from ccp.verify.environment_inspector import collect
from ccp.verify.transcript_inspector import inspect_files, render_markdown


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser with one sub-command per tool."""
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

    commands.add_parser("hook-probe", help="record one hook call from stdin (used by Claude Code)")
    commands.add_parser("probe-settings", help="print the settings that register the hook probe")
    report = commands.add_parser("probe-report", help="summarise what the hook probe recorded")
    report.add_argument("--log", type=Path, help="probe log file (default: see CCP_PROBE_LOG)")
    return parser


def _emit(text: str, out: Path | None) -> None:
    """Print the report, or write it to a file (UTF-8) when ``out`` is given."""
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(text)


def _run_transcript(paths: list[Path], out: Path | None) -> int:
    """Inspect transcripts and emit the Markdown report. Returns the exit code."""
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        print(f"Path not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    facts = inspect_files(paths)
    if not facts.files:  # a folder without any .jsonl file would otherwise give an empty report
        print("No .jsonl transcript files found.", file=sys.stderr)
        return 2
    _emit(render_markdown(facts), out)
    return 0


def _run_probe_report(log: Path | None) -> int:
    """Summarise the hook probe log. Returns the exit code."""
    log_path = log or hook_probe.default_log_path(os.environ)
    if not log_path.exists():
        print(f"Probe log not found: {log_path}", file=sys.stderr)
        return 2
    print(hook_probe.render_report(hook_probe.read_records(log_path)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 2 for a missing input. ``hook-probe`` always
        returns 0 so that it can never disturb Claude Code.
    """
    args = build_parser().parse_args(argv)

    if args.command == "transcript":
        return _run_transcript(args.paths, args.out)
    if args.command == "env":
        report_data = collect(os.environ, Path.home(), Path("/.dockerenv"))
        _emit(json.dumps(report_data, indent=2), args.out)
    elif args.command == "hook-probe":
        hook_probe.run(sys.stdin.read(), os.environ)
    elif args.command == "probe-settings":
        print(json.dumps(hook_probe.settings_snippet(hook_probe.probe_command()), indent=2))
    else:
        return _run_probe_report(args.log)
    return 0
