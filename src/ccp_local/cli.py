"""`ccp-local` commands: hook, install, uninstall, status, report, backfill."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import hook, store

EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
]
FILE_TOOLS = "Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell"
OURS = ("ccp_local", "ccp.capture")  # our hook, and the old server-upload hook it replaces


def _settings_path(arg: str | None) -> Path:
    return Path(arg) if arg else Path.home() / ".claude" / "settings.json"


def _strip_ours(hooks: dict) -> None:
    """Remove CCP hook entries, keep everything else the user has."""
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            rest = [
                h
                for h in group.get("hooks", [])
                if not any(mark in h.get("command", "") for mark in OURS)
            ]
            if rest:
                kept.append({**group, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]


def _save(path: Path, settings: dict) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def install(args: argparse.Namespace) -> int:
    """Add the CCP local hooks to settings.json (replacing old CCP hooks, keeping others)."""
    path = _settings_path(args.settings)
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    _strip_ours(hooks)
    package = Path(__file__).resolve().parent
    command = f'"{Path(sys.executable).as_posix()}" "{package.as_posix()}" hook'
    for event in EVENTS:
        group: dict = {"hooks": [{"type": "command", "command": command, "timeout": 30}]}
        if event == "PostToolUse":
            group["matcher"] = FILE_TOOLS
        hooks.setdefault(event, []).append(group)
    _save(path, settings)
    print(f"installed {len(EVENTS)} hooks -> captures in {store.home()}")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    """Remove the CCP hooks from settings.json."""
    path = _settings_path(args.settings)
    if not path.exists():
        return 0
    settings = json.loads(path.read_text(encoding="utf-8"))
    _strip_ours(settings.get("hooks", {}))
    _save(path, settings)
    print("CCP hooks removed")
    return 0


def status(_: argparse.Namespace) -> int:
    """Print captured projects with session and snapshot counts."""
    root = store.home()
    for pdir in sorted(p for p in root.glob("*__*") if p.is_dir()):
        sessions = list((pdir / "sessions").glob("*")) if (pdir / "sessions").exists() else []
        activity = store.read_jsonl(pdir / "activity.jsonl")
        last = activity[-1]["time"] if activity else "-"
        commits = sum(1 for a in activity if a.get("commit"))
        print(f"{pdir.name:40} sessions {len(sessions):3}  snapshots {commits:4}  last {last}")
    errors = store.read_jsonl(root / "errors.log")
    print(f"captures: {root}  errors logged: {len(errors)}")
    if errors:
        print(f"last error: {errors[-1]}")
    return 0


def _session_dirs(session_id: str | None) -> list[Path]:
    pins = store.home() / ".sessions"
    ids = [session_id] if session_id else [p.name for p in pins.glob("*")] if pins.exists() else []
    out = []
    for sid in ids:
        pin = pins / sid
        if pin.exists():
            pdir = Path(pin.read_text(encoding="utf-8").strip())
            out.extend((pdir / "sessions").glob(f"*__{sid[:8]}"))
    return out


def report_cmd(args: argparse.Namespace) -> int:
    """Rebuild capture.json and reports from what is already captured."""
    dirs = _session_dirs(args.session)
    for sdir in dirs:
        hook.rebuild(sdir)
        print(sdir / "report.html")
    if not dirs:
        print("no captured session found")
    return 0 if dirs else 1


def backfill(args: argparse.Namespace) -> int:
    """Import chats Claude Code already has on disk (no snapshots exist for those)."""
    source = Path(args.source) if args.source else Path.home() / ".claude" / "projects"
    count = 0
    for path in sorted(source.glob("*/*.jsonl")):
        cwd = sid = None
        for entry in store.read_jsonl(path):
            cwd, sid = cwd or entry.get("cwd"), sid or entry.get("sessionId")
            if cwd and sid:
                break
        if not (cwd and sid):
            continue
        pdir, _ = store.session_project(sid, cwd)
        first = next((e["timestamp"] for e in store.read_jsonl(path) if e.get("timestamp")), "")
        hook.rebuild(store.session_dir(pdir, sid, first[:10] or None), path)
        count += 1
    print(f"backfilled {count} sessions into {store.home()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one command."""
    parser = argparse.ArgumentParser(prog="ccp-local", description="Local Claude Code capture")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="run as a Claude Code hook (reads the event JSON on stdin)")
    for name in ("install", "uninstall"):
        p = sub.add_parser(name, help=f"{name} the hooks in ~/.claude/settings.json")
        p.add_argument("--settings", help="other settings.json (e.g. a project's .claude/)")
    sub.add_parser("status", help="captured projects, sessions, snapshots, errors")
    p = sub.add_parser("report", help="rebuild capture.json + reports (one session or all)")
    p.add_argument("session", nargs="?")
    p = sub.add_parser("rebuild", help="(used by the hook) rebuild one session folder")
    p.add_argument("dir")
    p.add_argument("transcript")
    p = sub.add_parser("backfill", help="import existing chats from ~/.claude/projects")
    p.add_argument("--source")
    args = parser.parse_args(argv)
    if args.cmd == "hook":
        return hook.run()
    if args.cmd == "rebuild":
        try:
            hook.rebuild(Path(args.dir), Path(args.transcript))
        except Exception as exc:
            store.log_error(f"rebuild {args.dir}: {type(exc).__name__}: {exc}")
        return 0
    handler = {
        "install": install,
        "uninstall": uninstall,
        "status": status,
        "report": report_cmd,
        "backfill": backfill,
    }[args.cmd]
    return handler(args)
