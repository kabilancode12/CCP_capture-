"""Probe that records *which* Claude Code hooks fire and what their payloads look like.

Several design questions depend on hook behaviour that is not documented well: does SessionEnd
fire on Windows, do the sub-agent hooks fire, do hooks run on every surface? Register this
probe on every hook, use Claude Code normally, then read the log with
``ccp-verify probe-report``.

Privacy: the probe never records content. Per hook call it stores the event name, the payload
**key names**, a few harmless enum values (see :data:`SAFE_VALUE_KEYS`), and whether each
``*_path`` value points to an existing file. Prompts, tool input and file paths are not stored.

Safety: the probe must never disturb Claude Code, so :func:`run` swallows every error and the
command always exits with code 0.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Hook events to register. [verify] Event names come from Claude Code's hook documentation;
# if Claude Code rejects one, remove it from this list.
PROBE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "Notification",
    "SessionEnd",
)

# Payload fields whose *values* are short enums or flags, safe to record.
# [verify] The set of fields differs per event; the probe records whichever are present.
SAFE_VALUE_KEYS = frozenset(
    {
        "hook_event_name",
        "source",  # SessionStart: startup | resume | clear | compact
        "reason",  # SessionEnd: why the session ended
        "trigger",  # PreCompact: manual | auto
        "permission_mode",
        "stop_hook_active",
        "tool_name",
        "agent_type",
        "agent_id",
    }
)

LOG_PATH_VARIABLE = "CCP_PROBE_LOG"


def default_log_path(env: Mapping[str, str]) -> Path:
    """Return the probe log location: ``$CCP_PROBE_LOG`` or ``~/.ccp/hook-probe.jsonl``."""
    override = env.get(LOG_PATH_VARIABLE)
    return Path(override) if override else Path.home() / ".ccp" / "hook-probe.jsonl"


def summarise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Describe a hook payload without exposing its content.

    Args:
        payload: The JSON object Claude Code sent to the hook on stdin.

    Returns:
        ``keys`` (sorted key names), ``values`` (only the safe enum-like fields) and
        ``paths_exist`` (for every key ending in ``_path``: does that file exist?).
    """
    values = {
        key: payload[key] for key in SAFE_VALUE_KEYS if isinstance(payload.get(key), str | bool)
    }
    paths_exist: dict[str, bool] = {}
    for key, value in payload.items():
        if key.endswith("_path") and isinstance(value, str):
            try:
                paths_exist[key] = Path(value).exists()
            except OSError:  # an invalid path counts as "not found"
                paths_exist[key] = False
    return {"keys": sorted(payload), "values": values, "paths_exist": paths_exist}


def build_record(payload: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Build the log record for one hook call.

    Args:
        payload: The hook payload.
        env: Environment variables, used only for the entry-point marker.
    """
    return {
        "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "pid": os.getpid(),
        "event": str(payload.get("hook_event_name", "<missing>")),
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT", ""),  # [verify] surface marker
        **summarise_payload(payload),
    }


def append_record(record: dict[str, Any], log_path: Path) -> None:
    """Append one record to the log (one JSON object per line), creating folders as needed."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def run(stdin_text: str, env: Mapping[str, str]) -> None:
    """Record one hook call. Never raises: a probe must not disturb Claude Code.

    If the payload cannot be parsed, an ``error`` record with only the error type is written.
    """
    log_path = default_log_path(env)
    try:
        payload = json.loads(stdin_text or "{}")
        if not isinstance(payload, dict):
            raise TypeError("payload is not a JSON object")
        append_record(build_record(payload, env), log_path)
    except Exception as error:  # noqa: BLE001  (deliberate: nothing may escape a hook)
        try:
            append_record({"event": "<probe-error>", "error": type(error).__name__}, log_path)
        except OSError:
            return  # even the log is unwritable; stay silent


def settings_snippet(command: str, timeout_seconds: int = 10) -> dict[str, Any]:
    """Build the Claude Code settings that register the probe on every event.

    Args:
        command: The full shell command that runs the probe.
        timeout_seconds: Per-hook time limit.

    Returns:
        A dictionary to save as ``.claude/settings.local.json`` in a *scratch* repository.
    """
    hook = {"type": "command", "command": command, "timeout": timeout_seconds}
    return {"hooks": {event: [{"hooks": [hook]}] for event in PROBE_EVENTS}}


def probe_command() -> str:
    """Return the shell command that runs the probe with the current Python.

    Forward slashes are used so the path also works when the hook runs in Git Bash.
    """
    python = Path(sys.executable).as_posix()
    return f'"{python}" -m ccp.verify hook-probe'


# ---------------------------------------------------------------- report


def read_records(log_path: Path) -> list[dict[str, Any]]:
    """Read every well-formed record from the log; blank or broken lines are skipped."""
    records: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def render_report(records: list[dict[str, Any]]) -> str:
    """Summarise the log as Markdown: one row per event, plus the order events fired in.

    Args:
        records: Records from :func:`read_records`.
    """
    counts: dict[str, int] = defaultdict(int)
    keys: dict[str, set[str]] = defaultdict(set)
    values: dict[str, set[str]] = defaultdict(set)
    for record in records:
        event = str(record.get("event", "<missing>"))
        counts[event] += 1
        keys[event].update(record.get("keys", []))
        for name in ("source", "reason", "trigger"):
            if name in record.get("values", {}):
                values[event].add(f"{name}={record['values'][name]}")

    entrypoints = sorted({str(r["entrypoint"]) for r in records if r.get("entrypoint")})
    lines = ["# Hook probe report", "", f"- Records: {len(records)}"]
    lines += [
        f"- Entrypoints seen (CLAUDE_CODE_ENTRYPOINT): {', '.join(entrypoints) or 'none'}",
        "",
    ]
    lines += ["| event | count | enum values seen | payload keys |", "| --- | --- | --- | --- |"]
    for event in sorted(counts):
        seen = ", ".join(sorted(values[event])) or "-"
        lines.append(f"| {event} | {counts[event]} | {seen} | {', '.join(sorted(keys[event]))} |")

    missing = [event for event in PROBE_EVENTS if event not in counts]
    lines += ["", f"Events that never fired: {', '.join(missing) or 'none'}", ""]
    lines += ["## Order of events", ""]
    lines += [f"{i}. {r.get('at', '?')} {r.get('event', '?')}" for i, r in enumerate(records, 1)]
    return "\n".join(lines) + "\n"
