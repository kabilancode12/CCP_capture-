"""Turn a session (raw transcript + hook events) into `capture.json`: one record per turn.

A turn starts at a real user prompt and holds everything until the next one: Claude's
thinking, replies and tool calls (with results, in order), models and tokens used, and the
shadow snapshots made during the turn (human edits before it, each tool's file changes).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import store

RESULT_LIMIT = 20000  # characters of a tool result kept in capture.json (raw/ keeps everything)
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")


def _text(content: object) -> str:
    """Plain text of a message/tool-result content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
                elif "content" in block:
                    parts.append(_text(block["content"]))
        return "\n".join(parts)
    return ""


def _is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    if entry.get("isCompactSummary"):
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str)


def _new_turn(n: int, entry: dict) -> dict:
    return {
        "turn": n, "started_at": entry.get("timestamp"), "ended_at": entry.get("timestamp"),
        "prompt": _text(entry["message"]["content"]), "models": [],
        "tokens": dict.fromkeys(TOKEN_KEYS, 0), "steps": [], "changes": [],
    }


def build(sdir: Path) -> dict:
    """Build and write `capture.json` for one session folder; return it."""
    entries = store.read_jsonl(sdir / "raw" / "transcript.jsonl")
    events = store.read_jsonl(sdir / "events.jsonl")
    session: dict = {"session_id": None, "title": None, "cwd": None, "git_branch": None,
                     "claude_version": None, "compactions": 0}
    turns: list[dict] = []
    tools: dict[str, dict] = {}  # tool_use_id -> step, to attach results
    seen_messages: set[str] = set()  # usage is repeated on every block of one API message

    for entry in entries:
        kind = entry.get("type")
        if kind in ("ai-title", "custom-title", "summary"):
            session["title"] = entry.get("aiTitle") or entry.get("title") or entry.get("summary")
        if entry.get("sessionId") and not session["session_id"]:
            session["session_id"] = entry["sessionId"]
        for key, src in (("cwd", "cwd"), ("git_branch", "gitBranch"), ("claude_version", "version")):
            if entry.get(src):
                session[key] = entry[src]
        if entry.get("isCompactSummary"):
            session["compactions"] += 1
        if kind not in ("user", "assistant") or entry.get("isSidechain"):
            continue
        if _is_prompt(entry):
            turns.append(_new_turn(len(turns) + 1, entry))
            continue
        if not turns:
            continue
        turn = turns[-1]
        turn["ended_at"] = entry.get("timestamp") or turn["ended_at"]
        message = entry.get("message", {})
        content = message.get("content")
        if kind == "user":  # tool results
            for block in content if isinstance(content, list) else []:
                step = tools.get(block.get("tool_use_id", "")) if isinstance(block, dict) else None
                if step is not None:
                    step["result"] = _text(block.get("content"))[:RESULT_LIMIT]
                    step["is_error"] = bool(block.get("is_error"))
            continue
        model = message.get("model")
        if model and model != "<synthetic>" and model not in turn["models"]:
            turn["models"].append(model)
        if message.get("id") not in seen_messages:
            seen_messages.add(message.get("id"))
            for key in TOKEN_KEYS:
                turn["tokens"][key] += (message.get("usage") or {}).get(key) or 0
        for block in content if isinstance(content, list) else []:
            btype = block.get("type")
            if btype == "thinking" and block.get("thinking"):
                turn["steps"].append({"kind": "thinking", "text": block["thinking"]})
            elif btype == "text" and block.get("text", "").strip():
                turn["steps"].append({"kind": "text", "text": block["text"]})
            elif btype == "tool_use":
                step = {"kind": "tool", "id": block.get("id"), "name": block.get("name"),
                        "input": block.get("input"), "result": None, "is_error": False}
                tools[block.get("id")] = step
                turn["steps"].append(step)

    _attach_snapshots(turns, events, tools)
    capture = {"session": session, "turn_count": len(turns), "turns": turns,
               "events": len(events), "built_at": store.now()}
    store.write_json(sdir / "capture.json", capture)
    return capture


def _attach_snapshots(turns: list[dict], events: list[dict], tools: dict[str, dict]) -> None:
    """Put each snapshot in the turn it happened in, and on the tool step that made it."""
    for event in events:
        snap = event.get("snapshot")
        if not snap:
            continue
        step = tools.get(event.get("tool_use_id") or "")
        if step is not None:
            step["snapshot"] = snap["commit"][:10]
        change = {"time": event["time"], "by": event.get("by"), "label": event.get("label"),
                  **snap}
        # the prompt snapshot is taken just before the prompt is written: allow 5 s
        target = None
        for turn in turns:
            if turn["started_at"] and _secs(turn["started_at"]) <= _secs(event["time"]) + 5:
                target = turn
        if target is not None:
            target["changes"].append(change)


def _secs(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def dumps_input(value: object) -> str:
    """Readable tool input for reports."""
    if isinstance(value, dict) and set(value) <= {"command", "description", "timeout"}:
        return str(value.get("command", ""))
    return json.dumps(value, indent=2, ensure_ascii=False)
