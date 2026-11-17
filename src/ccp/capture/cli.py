"""The ``ccp-capture`` command: upload transcripts from a developer's machine.

    ccp-capture sync --session-id <id> <transcript.jsonl>   upload what is new for one session
    ccp-capture catch-up                                    upload what earlier sessions left behind
    ccp-capture environment --session-id <id> [transcript]  record where the session runs
    ccp-capture snapshot --session-id <id> [--cwd DIR]      take a code snapshot now
    ccp-capture flush --session-id <id> [--cwd DIR] [file]  add the Git HEAD to the session end,
                                                            send it, upload the rest, then snapshot
    ccp-capture hook                                        handle one Claude Code hook (stdin)
    ccp-capture hook-settings [--api-url URL]               print the hook registration settings
    ccp-capture install --api-url URL --token TOKEN         add the hooks and settings
    ccp-capture uninstall                                   remove only our hooks and settings
    ccp-capture status                                      is it installed, reachable, uploading
    ccp-capture backfill [--dry-run] [--since DAYS]         upload the chats that already exist

Settings come from environment variables only (see docs/configuration.md); a ``.env`` file is
deliberately not read. ``backfill`` and ``status`` also use the values ``install`` wrote to Claude
Code's settings file (the shell's own variables win). ``install``, ``uninstall``, ``backfill`` and
``status`` are described in docs/install.md. The Claude Code hooks (task 1.9) call these commands.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.capture.api import HttpApiClient
from ccp.capture.backfill import ProjectReport, backfill, default_projects_dir
from ccp.capture.client import API_UNAVAILABLE, ENVIRONMENT_PARTIAL, CaptureClient, StreamResult
from ccp.capture.environment import collect_environment
from ccp.capture.gitinfo import read_head
from ccp.capture.hook_runner import run_hook
from ccp.capture.hook_settings import build_hook_settings, hook_command
from ccp.capture.install import (
    ENV_TOKEN,
    InstallError,
    collect_status,
    default_settings_path,
    effective_env,
    install,
    uninstall,
)
from ccp.capture.snapshots.context import HUMAN, SnapshotContext
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
    environment.add_argument("--cwd", type=Path, help="the directory Claude Code works in")
    flush = commands.add_parser(
        "flush", help="send the session end, upload what is left and take the final snapshot"
    )
    flush.add_argument("--session-id", required=True, help="Claude Code session id")
    flush.add_argument("--cwd", type=Path, help="the directory Claude Code worked in")
    flush.add_argument(
        "transcript", nargs="?", type=Path, help="the session transcript (.jsonl), if there is one"
    )
    snapshot = commands.add_parser("snapshot", help="take a code snapshot of a repository now")
    snapshot.add_argument("--session-id", required=True, help="Claude Code session id")
    snapshot.add_argument("--cwd", type=Path, default=Path.cwd(), help="a folder in the repository")
    snapshot.add_argument("--event", default="Manual", help="recorded in the snapshot message")
    commands.add_parser("hook", help="handle one Claude Code hook (payload on standard input)")
    watch = commands.add_parser(
        "watch", help="watch the repositories for changes no hook sees (run in the background)"
    )
    watch.add_argument("--interval", type=float, default=30.0, help="seconds between looks")
    watch.add_argument("--quiet", type=float, default=60.0, help="seconds without change first")
    watch.add_argument("--once", action="store_true", help="look once and stop")
    watch_service = commands.add_parser(
        "watch-service", help="start the watcher at login (systemd, launchd or Task Scheduler)"
    )
    watch_service.add_argument("action", choices=["install", "uninstall"])
    git_hooks = commands.add_parser("git-hooks", help="install or remove the global Git hooks")
    git_hooks.add_argument("action", choices=["install", "uninstall"])
    git_hook = commands.add_parser("git-hook", help="run by the Git hook scripts (not by hand)")
    git_hook.add_argument("name", choices=["post-commit", "pre-push"])
    hook_settings = commands.add_parser(
        "hook-settings", help="print the settings that register hooks"
    )
    hook_settings.add_argument(
        "--api-url", default="http://127.0.0.1:8000", help="ingestion API address for the env block"
    )
    install_parser = commands.add_parser(
        "install", help="add the capture hooks and settings to Claude Code's settings.json"
    )
    install_parser.add_argument("--api-url", required=True, help="ingestion API address")
    install_parser.add_argument(
        "--token", help="the ingest token (default: the CCP_INGEST_TOKEN variable)"
    )
    commands.add_parser("uninstall", help="remove only the capture hooks and settings")
    commands.add_parser("status", help="show whether capture is installed and working")
    backfill_parser = commands.add_parser(
        "backfill", help="upload the chats that already exist on this machine"
    )
    backfill_parser.add_argument(
        "--dry-run", action="store_true", help="only count what would be uploaded"
    )
    backfill_parser.add_argument(
        "--since", type=float, metavar="DAYS", help="only chats changed in the last DAYS days"
    )
    backfill_parser.add_argument(
        "--projects-dir",
        type=Path,
        help="Claude Code's projects folder (default ~/.claude/projects)",
    )
    for name in ("install", "uninstall", "status", "backfill"):
        commands.choices[name].add_argument(
            "--settings-path",
            type=Path,
            help="Claude Code settings file (default ~/.claude/settings.json)",
        )
    return parser


def _summary(session_id: str, results: Sequence[StreamResult]) -> tuple[str, bool]:
    """Describe the results of one session in a line; also say whether all went well."""
    chunks = sum(r.chunks for r in results)
    attachments = sum(r.attachments for r in results)
    files = sum(r.files for r in results)
    errors = [f"{r.stream}: {r.error}" for r in results if r.error]
    line = f"{session_id}: {chunks} chunk(s), {attachments} attachment(s)"
    line += f", {files} tool result(s) uploaded" if files else " uploaded"
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
    if args.command == "watch-service":
        from ccp.capture import watch_service

        notes = watch_service.install() if args.action == "install" else watch_service.uninstall()
        print("\n".join(notes))
        return 0
    if args.command == "git-hooks":
        from ccp.capture import githooks

        notes = githooks.install() if args.action == "install" else githooks.uninstall()
        print("\n".join(notes))
        return 0
    if args.command == "hook-settings":
        print(json.dumps(build_hook_settings(hook_command(), args.api_url), indent=2))
        return 0
    if args.command in ("install", "uninstall", "status"):
        return _run_install_command(args)
    try:
        settings = load_client_settings(
            effective_env(
                getattr(args, "settings_path", None) or default_settings_path(), os.environ
            )
            if args.command in ("backfill", "watch")
            else None
        )
    except SettingsError as error:
        print(error, file=sys.stderr)
        return 1
    state = StateStore(settings.state_dir)
    api = HttpApiClient(settings.api_url, settings.token, settings.timeout_seconds, fail_fast=True)
    client = CaptureClient(
        api,
        state,
        chunk_bytes=settings.chunk_bytes,
        budget_seconds=settings.budget_seconds,
    )
    snapshots = snapshot_service_from(settings, state, api)

    if args.command == "backfill":
        return _run_backfill(args, client, state)

    if args.command == "git-hook":  # run by Git: silent, never fails
        if args.name == "post-commit":
            snapshots.note_commit(Path.cwd())
        else:
            snapshots.backup(Path.cwd())
        return 0

    if args.command == "watch":
        from ccp.capture.watcher import Watcher, run

        watcher = Watcher(snapshots, state, settings.state_dir, quiet_seconds=args.quiet)
        with contextlib.suppress(KeyboardInterrupt):
            run(watcher, args.interval, once=args.once)
        return 0

    if args.command == "snapshot":
        outcome = snapshots.snapshot(args.session_id, args.cwd, args.event, SnapshotContext(HUMAN))
        print(f"{args.session_id}: " + (outcome or "snapshots are not enabled (see CCP_SNAPSHOTS)"))
        return 0 if outcome is None or not outcome.startswith("failed") else 1

    if args.command == "flush":  # started by the SessionEnd hook, in the background
        if args.cwd is not None:  # the hook had no time to ask Git
            client.add_end_commit(args.session_id, read_head(args.cwd))
        sent = client.send_lifecycle(args.session_id)
        results = (
            client.sync_session(args.session_id, args.transcript)
            if args.transcript is not None
            else []
        )
        line, flushed_ok = _summary(args.session_id, results)
        # A chat closed without a message (no transcript) changed no code: no final snapshot.
        note = (
            snapshots.finish(args.session_id, args.cwd, SnapshotContext(HUMAN))
            if args.cwd is not None and args.transcript is not None
            else None
        )
        print(line + (f"; {sent}" if sent else "") + (f"; code: {note}" if note else ""))
        return 0 if flushed_ok and sent != API_UNAVAILABLE else 1

    if args.command == "environment":
        record = collect_environment(os.environ, Path.home(), args.transcript, cwd=args.cwd)
        problem = client.report_environment(args.session_id, record)
        if problem == ENVIRONMENT_PARTIAL:
            print(f"{args.session_id}: environment {problem}")
            return 0
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


def _run_install_command(args: argparse.Namespace) -> int:
    """Run ``install``, ``uninstall`` or ``status``. The token is never printed."""
    path = args.settings_path or default_settings_path()
    if args.command == "status":
        status = collect_status(path, os.environ)

        def word(value: bool | None, yes: str = "yes", no: str = "no") -> str:
            return "unknown" if value is None else (yes if value else no)

        print(f"installed:       {word(status.installed)}", end="")
        print(
            f" (no hook for: {', '.join(status.missing_events)})" if status.missing_events else ""
        )
        print(f"api reachable:   {word(status.reachable)}")
        print(f"token valid:     {word(status.token_valid)}")
        print(f"pending uploads: {status.pending_uploads}")
        last = status.last_upload
        print(f"last upload:     {last.strftime('%Y-%m-%d %H:%M:%S %Z') if last else 'never'}")
        return 0
    try:
        if args.command == "uninstall":
            result = uninstall(path)
            message = (
                "removed the capture hooks and settings from"
                if result.changed
                else "nothing to remove in"
            )
        else:
            token = args.token or os.environ.get(ENV_TOKEN, "")
            result = install(path, args.api_url, token)
            message = (
                "installed the capture hooks and settings in"
                if result.changed
                else "already installed in"
            )
    except InstallError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{message} {path}")
    if result.backup is not None:
        print(f"backup of the old file: {result.backup}")
    return 0


def _run_backfill(args: argparse.Namespace, client: CaptureClient, state: StateStore) -> int:
    """Run ``backfill`` and print one line per project, then a total."""

    def show(project: str, report: ProjectReport) -> None:
        verb = "would upload" if args.dry_run else "uploaded"
        line = (
            f"{project}: {report.found} chat(s), {verb} {report.uploaded}, "
            f"already done {report.unchanged}"
        )
        print(line + (f", incomplete {report.incomplete}" if report.incomplete else ""))

    report = backfill(
        client,
        state,
        args.projects_dir or default_projects_dir(),
        since_days=args.since,
        dry_run=args.dry_run,
        on_project=show,
    )
    found = sum(p.found for p in report.projects.values())
    if not found:
        print("no chats found")
    for error in report.errors:
        print(f"problem: {error}", file=sys.stderr)
    if report.stopped:
        print(f"stopped early: {report.stopped}", file=sys.stderr)
    return 0 if report.ok else 1
