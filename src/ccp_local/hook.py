"""The Claude Code hook: called for each event with the event's JSON on stdin.

Per event: (1) take a shadow snapshot when files may have changed, (2) append the payload to
the session's events.jsonl and a short line to the project's activity.jsonl, (3) at turn/
session end copy the raw transcript and rebuild capture.json + reports.
A hook must never break Claude Code: every error is logged and the hook exits 0, silently.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import report, shadow, store, transcript

# event -> (who changed the files, label). PostToolUse is only registered for file tools.
SNAPSHOT_EVENTS = {
    "SessionStart": ("human", "before session"),
    "UserPromptSubmit": ("human", "before prompt"),
    "PostToolUse": ("claude", "tool"),
    "Stop": ("other", "after turn"),
}
BUILD_EVENTS = {"Stop", "SubagentStop", "PreCompact", "SessionEnd"}


def handle(payload: dict) -> None:
    """Record one hook event (see the module docstring)."""
    sid = payload.get("session_id")
    if not sid or os.environ.get("CCP_CAPTURE_DISABLED") == "1":
        return
    event = payload.get("hook_event_name", "")
    pdir, project = store.session_project(sid, payload.get("cwd") or os.getcwd())
    sdir = store.session_dir(pdir, sid)
    tool = payload.get("tool_name")
    record = {
        "time": store.now(),
        "event": event,
        "tool": tool,
        "tool_use_id": payload.get("tool_use_id"),
        "by": None,
        "label": None,
        "snapshot": None,
    }

    if event in SNAPSHOT_EVENTS and project.get("snapshots"):
        by, label = SNAPSHOT_EVENTS[event]
        label = tool if event == "PostToolUse" else label
        trailers = {
            "Session": sid,
            "Event": event,
            "Tool": tool,
            "Tool-Use-Id": payload.get("tool_use_id"),
            "Prompt": (payload.get("prompt") or "")[:300].replace("\n", " "),
        }
        try:
            snap = shadow.snapshot(pdir, Path(project["root"]), by, f"[{by}] {label}", trailers)
        except Exception as exc:  # a failed snapshot must not lose the event itself
            store.log_error(f"snapshot {sid} {event}: {exc}")
            snap = None
        if snap:
            record.update(by=by, label=label, snapshot=snap)

    store.append_jsonl(sdir / "events.jsonl", {**record, "payload": payload})
    store.append_jsonl(
        pdir / "activity.jsonl",
        {
            "time": record["time"],
            "session": sid,
            "event": event,
            "tool": tool,
            "commit": (record["snapshot"] or {}).get("commit"),
            "files": (record["snapshot"] or {}).get("file_count", 0),
        },
    )

    if event in BUILD_EVENTS and payload.get("transcript_path"):
        rebuild_in_background(sdir, payload["transcript_path"])


def rebuild_in_background(sdir: Path, transcript_path: str) -> None:
    """Rebuild in a detached process so Claude Code never waits (big chats take seconds)."""
    if os.environ.get("CCP_LOCAL_SYNC") == "1":  # tests: do it now
        rebuild(sdir, Path(transcript_path))
        return
    cmd = [sys.executable, str(Path(__file__).resolve().parent), "rebuild", str(sdir),
           transcript_path]
    detach = (
        {"creationflags": 0x00000008 | 0x00000200}  # Windows: DETACHED + NEW_PROCESS_GROUP
        if os.name == "nt" else {"start_new_session": True}
    )
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **detach)


def rebuild(sdir: Path, transcript_path: Path | None = None) -> dict:
    """Refresh raw/ from Claude Code's files (when given), then capture.json and the reports."""
    if transcript_path is not None:
        store.copy_raw(transcript_path, sdir)
    cap = transcript.build(sdir)
    report.write(sdir, cap)
    return cap


def run() -> int:
    """Entry for `ccp-local hook`: read stdin, handle, never fail."""
    try:
        handle(json.loads(sys.stdin.read() or "{}"))
    except Exception as exc:
        store.log_error(f"hook: {type(exc).__name__}: {exc}")
    return 0
