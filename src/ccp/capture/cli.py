"""The ``ccp-capture`` command: upload transcripts from a developer's machine.

    ccp-capture sync --session-id <id> <transcript.jsonl>   upload what is new for one session
    ccp-capture catch-up                                    upload what earlier sessions left behind

Settings come from environment variables only (see docs/configuration.md); a ``.env`` file is
deliberately not read. The Claude Code hooks (task 1.9) call these commands.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.capture.api import HttpApiClient
from ccp.capture.client import CaptureClient, StreamResult
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
    try:
        settings = load_client_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    client = CaptureClient(
        HttpApiClient(settings.api_url, settings.token, settings.timeout_seconds),
        StateStore(settings.state_dir),
        chunk_bytes=settings.chunk_bytes,
        budget_seconds=settings.budget_seconds,
    )

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
