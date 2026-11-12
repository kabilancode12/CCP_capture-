"""The ``ccp-capture`` command: upload transcripts from a developer's machine.

    ccp-capture sync --session-id <id> <transcript.jsonl>   upload what is new for one session
    ccp-capture catch-up                                    upload what earlier sessions left behind
    ccp-capture environment --session-id <id> [transcript]  record where the session runs
    ccp-capture snapshot --session-id <id> [--cwd DIR]      take a code snapshot now
    ccp-capture flush --session-id <id> [--cwd DIR] <file>  upload the rest, then snapshot
    ccp-capture hook                                        handle one Claude Code hook (stdin)
    ccp-capture hook-settings [--api-url URL]               print the hook registration settings

Settings come from environment variables only (see docs/configuration.md); a ``.env`` file is
deliberately not read. The Claude Code hooks (task 1.9) call these commands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.capture.api import HttpApiClient
from ccp.capture.client import CaptureClient, StreamResult
from ccp.capture.environment import collect_environment
from ccp.capture.hook_runner import run_hook
from ccp.capture.hook_settings import build_hook_settings, hook_command
from ccp.capture.snapshots.service import snapshot_service_from
from ccp.capture.state import StateStore
from ccp.settings import SettingsError, load_client_settings


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-capture", description="Upload Claude Code sessions.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="upload what is new for one session")
    sync.add_argument("--session-id", required=True, help="Claude Code session id")
    sync.add_argument("transcript", type=Path, help="the session transcript (.jsonl)")
    commands.add_parser("catch-up", help="upload what earlier sessions left behind")
    environment = commands.add_parser("environment", help="record where the session runs")
    environment.add_argument("--session-id", required=True, help="Claude Code session id")
    environment.add_argument(
        "transcript", nargs="?", type=Path, help="the session transcript, once it exists"
    )
    flush = commands.add_parser("flush", help="upload what is left and take the final snapshot")
    flush.add_argument("--session-id", required=True, help="Claude Code session id")
    flush.add_argument("--cwd", type=Path, help="the directory Claude Code worked in")
    flush.add_argument("transcript", type=Path, help="the session transcript (.jsonl)")
    snapshot = commands.add_parser("snapshot", help="take a code snapshot of a repository now")
    snapshot.add_argument("--session-id", required=True, help="Claude Code session id")
    snapshot.add_argument("--cwd", type=Path, default=Path.cwd(), help="a folder in the repository")
    snapshot.add_argument("--event", default="Manual", help="recorded in the snapshot message")
    commands.add_parser("hook", help="handle one Claude Code hook (payload on standard input)")
    hook_settings = commands.add_parser(
        "hook-settings", help="print the settings that register hooks"
    )
    hook_settings.add_argument(
        "--api-url", default="http://127.0.0.1:8000", help="ingestion API address for the env block"
    )
    return parser


def _summary(session_id: str, results: Sequence[StreamResult]) -> tuple[str, bool]:
    """Describe the results of one session in a line; also say whether all went well."""
    chunks = sum(r.chunks for r in results)
    attachments = sum(r.attachments for r in results)
    errors = [f"{r.stream}: {r.error}" for r in results if r.error]
    line = f"{session_id}: {chunks} chunk(s), {attachments} attachment(s) uploaded"
    if errors:
        line += "; " + "; ".join(errors)
    return line, not errors


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Returns:
        0 if everything uploaded (or there was nothing to do), 1 if a settings problem or an
        upload error occurred. The hook wrapper (task 1.9) decides what to do with a non-zero code.
    """
    args = build_parser().parse_args(argv)
    if args.command == "hook":  # never fails, never prints: see ccp.capture.hook_runner
        payload = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return run_hook(payload, os.environ, Path.home())
    if args.command == "hook-settings":
        print(json.dumps(build_hook_settings(hook_command(), args.api_url), indent=2))
        return 0
    try:
        settings = load_client_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    state = StateStore(settings.state_dir)
    client = CaptureClient(
        HttpApiClient(settings.api_url, settings.token, settings.timeout_seconds),
        state,
        chunk_bytes=settings.chunk_bytes,
        budget_seconds=settings.budget_seconds,
    )
    snapshots = snapshot_service_from(settings, state)

    if args.command == "snapshot":
        outcome = snapshots.snapshot(args.session_id, args.cwd, args.event)
        print(f"{args.session_id}: " + (outcome or "snapshots are not enabled (see CCP_SNAPSHOTS)"))
        return 0 if outcome is None or not outcome.startswith("failed") else 1

    if args.command == "flush":  # started by the SessionEnd hook, in the background
        results = client.sync_session(args.session_id, args.transcript)
        line, flushed_ok = _summary(args.session_id, results)
        note = snapshots.finish(args.session_id, args.cwd) if args.cwd is not None else None
        print(line + (f"; code: {note}" if note else ""))
        return 0 if flushed_ok else 1

    if args.command == "environment":
        record = collect_environment(os.environ, Path.home(), args.transcript)
        problem = client.report_environment(args.session_id, record)
        print(
            f"{args.session_id}: environment "
            + (f"not sent ({problem})" if problem else "recorded")
        )
        return 1 if problem else 0

    if args.command == "sync":
        sessions = {args.session_id: client.sync_session(args.session_id, args.transcript)}
    else:
        sessions = client.catch_up()

    ok = True
    for session_id, results in sessions.items():
        line, session_ok = _summary(session_id, results)
        print(line)
        ok = ok and session_ok
    if not sessions:
        print("nothing to do")
    return 0 if ok else 1
