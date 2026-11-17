"""The files of one chat folder: the JSON documents and the README.

Each ``build_*`` function turns the facts of :class:`~ccp.catalog.loader.ChatData` into plain
JSON-ready data. Nothing here reads a database or an object store, so every function can be tested
with hand-made data. The files never contain e-mail addresses: an account is named by its label or
display name.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ccp.catalog.loader import ChatData, Row

TEXT_MAX = 2000  # sub-agent instructions and results are cut to this many characters

FILES = {
    "README.md": "what this chat is and what every file means",
    "report.md": "the whole chat in one readable file: every file below, as headings and tables",
    "00_summary.json": "ids, account, project, title, start and end, models, tokens, cost, counts",
    "01_conversation.md": "the conversation, readable: prompts, replies, tool calls, thinking",
    "02_sessions.json": "each time the chat was opened (start, resume, clear, compact)",
    "03_decisions.json": "the decisions found in the chat and who reviewed them",
    "04_tests.json": "test runs and the single test cases in them",
    "05_code_changes.json": "file changes, linked commits, capture commits (who made each)",
    "06_corrections.json": "things that had to be put right (failed tests fixed, retries, rework)",
    "07_subagents_and_workflows.json": "sub-agents, workflow runs and slash commands",
    "08_usage.json": "tokens, effort and cost per model",
    "transcript/main.json": "the raw lines of the main transcript, as a JSON array",
    "transcript/agents/": "the raw lines of each sub-agent, one file per agent",
}


def to_json(value: Any, sort_keys: bool = False) -> bytes:
    """Pretty-printed JSON (indent 2, UTF-8, not ASCII-escaped) with a final newline."""
    text = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=sort_keys, default=_default)
    return (text + "\n").encode("utf-8")


def _default(value: Any) -> Any:
    """How JSON writes what the database returns: times as ISO text, numbers as numbers."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set | tuple):
        return list(value)
    raise TypeError(f"cannot write {type(value).__name__} as JSON")


def ref(key: str | None, line: int | None) -> dict[str, Any] | None:
    """Where a fact is in the raw data: the object key and the line number in that object."""
    return {"key": key, "line": line} if key else None


def _int(value: Any) -> int:
    return int(value) if value is not None else 0


def _tokens(rows: list[Row]) -> dict[str, int]:
    """Token totals over usage rows; real models only (not the ``<synthetic>`` placeholder)."""
    real = [r for r in rows if r["model"] and r["model"] != "<synthetic>"]
    out = {
        "input": sum(_int(r["input_tokens"]) for r in real),
        "output": sum(_int(r["output_tokens"]) for r in real),
        "cache_read": sum(_int(r["cache_read_tokens"]) for r in real),
        "cache_creation": sum(_int(r["cache_creation_tokens"]) for r in real),
    }
    out["total"] = sum(out.values())
    return out


def _tool_call_count(data: ChatData) -> int:
    count = 0
    for event in data.events:
        content = event["content"]
        if event["is_abandoned"] or not isinstance(content, list):
            continue
        count += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    return count


def build_summary(data: ChatData, folder_path: str) -> dict[str, Any]:
    """``00_summary.json``: who, where, when, how much."""
    chat, events = data.chat, data.events
    models = {r["model"] for r in data.usage_by_model + data.cost_by_model if r["model"]}
    models.discard("<synthetic>")
    cost = data.cost_total["total_cost_usd"] if data.cost_total else None
    runs = data.test_runs
    return {
        "chat_id": chat["chat_id"],
        "title": chat["title"],
        "catalog_path": folder_path,
        "account": {
            "account_id": chat["account_id"],
            "account_uuid": chat["account_uuid"],
            "label": chat["account_label"],
            "name": chat["account_name"],
        },
        "project": {
            "project_id": chat["project_id"],
            "key": chat["project_key"],
            "name": chat["project_name"],
            "label": chat["project_label"],
            "git_remote": chat["git_remote"],
        },
        "folder": chat["folder"],
        "branch": chat["branch"],
        "start_commit": chat["start_commit"],
        "surface": chat["surface"],
        "status": chat["status"],
        "started_at": chat["started_at"],
        "ended_at": chat["ended_at"],
        "last_activity_at": chat["last_activity_at"],
        "models": sorted(models),
        "tokens": _tokens(data.usage_by_model),
        "cost_usd": cost,
        "counts": {
            "sessions": len(data.sessions),
            "prompts": sum(
                1
                for e in events
                if e["event_type"] == "prompt" and e["actor"] == "human" and not e["is_abandoned"]
            ),
            "replies": sum(
                1 for e in events if e["event_type"] == "response" and not e["is_abandoned"]
            ),
            "tool_calls": _tool_call_count(data),
            "decisions": sum(1 for d in data.decisions if d["status"] != "rejected"),
            "test_runs": len(runs),
            "tests_passed": sum(_int(r["tests_passed"]) for r in runs),
            "tests_failed": sum(_int(r["tests_failed"]) for r in runs),
            "file_changes": len(data.file_changes),
            "files_changed": len({c["path"] for c in data.file_changes}),
            "corrections": len(data.corrections),
            "agents": len(data.agents),
            "workflows": len(data.workflows),
            "commits": len(data.commits),
        },
    }


def build_sessions(data: ChatData) -> list[Row]:
    """``02_sessions.json``: each time the chat was opened."""
    return data.sessions


def build_decisions(data: ChatData) -> list[dict[str, Any]]:
    """``03_decisions.json``: one clear record per decision, in the order they were made.

    The keys are plain words: what was wanted (``goal``), what else was possible (``options``),
    what was done (``chosen``), why (``reason``), what came out (``result``), the tests
    (``tests``), the files it changed (``files_changed``), who decided (``decided_by``) and who
    checked it (``review``).
    """
    return [
        {
            "number": d["seq"],
            "title": d["title"],
            "goal": d["goal"],
            "prompt": {
                "text": d.get("prompt_text"),
                "at": d.get("prompt_at"),
                "entry": d.get("prompt_uuid"),
            },
            "options": d["options"] or [],
            "chosen": d["chosen"],
            "reason": d["reason"],
            "result": d["result"],
            "tests": d["tests_summary"],
            "files_changed": [
                {"file": f.get("path"), "change": f.get("change"), "how": f.get("via")}
                for f in (d["files"] or [])
                if isinstance(f, dict)
            ],
            "decided_by": d["decided_by"],
            "thinking_notes": [
                {"note": t.get("text"), "transcript_file": t.get("key"), "line": t.get("line")}
                for t in (d["thinking"] or [])
                if isinstance(t, dict)
            ],
            "commits": d["commit_shas"] or [],
            "review": {
                "status": d["status"],
                "checked_by": d["reviewed_by_kind"],
                "reviewer": d["reviewer"],
                "note": d["review_note"],
                "history": d["review_history"],
            },
            "evidence": [
                {"transcript_file": e.get("key"), "line": e.get("line")}
                for e in (d["evidence_lines"] or [])
                if isinstance(e, dict)
            ],
            "written_by": d["model"],
            "llm_usage": d["llm_usage"] or {},
            "decision_id": d["decision_id"],
            "created_at": d["created_at"],
        }
        for d in data.decisions
    ]


def build_tests(data: ChatData) -> dict[str, Any]:
    """``04_tests.json``: test runs, each with its test cases."""
    cases: dict[int, list[Row]] = {}
    for case in data.test_cases:
        cases.setdefault(case["test_run_id"], []).append(
            {k: case[k] for k in ("position", "name", "outcome", "message")}
        )
    runs = []
    for run in data.test_runs:
        runs.append(
            {
                **{k: v for k, v in run.items() if k not in ("raw_object_key", "raw_line_number")},
                "raw": ref(run["raw_object_key"], run["raw_line_number"]),
                "cases": cases.get(run["test_run_id"], []),
            }
        )
    return {
        "summary": {
            "runs": len(runs),
            "runs_passed": sum(1 for r in data.test_runs if r["passed"]),
            "runs_failed": sum(1 for r in data.test_runs if r["passed"] is False),
            "tests_passed": sum(_int(r["tests_passed"]) for r in data.test_runs),
            "tests_failed": sum(_int(r["tests_failed"]) for r in data.test_runs),
            "tests_skipped": sum(_int(r["tests_skipped"]) for r in data.test_runs),
        },
        "runs": runs,
    }


def build_code_changes(data: ChatData) -> dict[str, Any]:
    """``05_code_changes.json``: file changes with their role, per-role totals and commits."""
    changes = [
        {
            **{k: v for k, v in c.items() if k not in ("raw_object_key", "raw_line_number")},
            "raw": ref(c["raw_object_key"], c["raw_line_number"]),
        }
        for c in data.file_changes
    ]
    by_role: Counter[str] = Counter(c["file_role"] or "unknown" for c in data.file_changes)
    return {
        "summary": {
            "changes": len(changes),
            "files": len({c["path"] for c in data.file_changes}),
            "lines_added": sum(_int(c["lines_added"]) for c in data.file_changes),
            "lines_removed": sum(_int(c["lines_removed"]) for c in data.file_changes),
            "tests_added": sum(_int(c["tests_added"]) for c in data.file_changes),
            "changes_by_role": dict(sorted(by_role.items())),
            "capture_commits": len(data.shadow_commits),
        },
        "changes": changes,
        "commits": data.commits,
        "capture_commits": data.shadow_commits,
    }


def build_corrections(data: ChatData) -> list[Row]:
    """``06_corrections.json``: each correction with the raw lines that show it."""
    return [
        {
            "correction_id": k["correction_id"],
            "kind": k["kind"],
            "corrected_by": k["corrected_by"],
            "detected_by": k["detected_by"],
            "note": k["note"],
            "session_seq": k["session_seq"],
            "trigger_at": k["trigger_at"],
            "trigger": ref(k["trigger_key"], k["trigger_line"]),
            "fix_at": k["fix_at"],
            "fix": ref(k["fix_key"], k["fix_line"]),
        }
        for k in data.corrections
    ]


def _cut(text: str | None) -> str | None:
    return text if text is None or len(text) <= TEXT_MAX else text[:TEXT_MAX] + "..."


def build_subagents(data: ChatData) -> dict[str, Any]:
    """``07_subagents_and_workflows.json``: agents, workflow runs, slash commands."""
    agents = [
        {**a, "instructions": _cut(a["instructions"]), "result": _cut(a["result"])}
        for a in data.agents
    ]
    return {
        "agents": agents,
        "workflows": data.workflows,
        "slash_commands": [
            {k: s[k] for k in ("occurred_at", "is_abandoned", "name", "arguments")}
            for s in data.slash_commands
        ],
    }


def build_usage(data: ChatData) -> dict[str, Any]:
    """``08_usage.json``: tokens and effort per model, and the cost."""
    efforts = Counter(
        r["effort"] or "unknown" for r in data.usage_by_model for _ in range(_int(r["responses"]))
    )
    return {
        "total": {
            "tokens": _tokens(data.usage_by_model),
            "cost_usd": data.cost_total["total_cost_usd"] if data.cost_total else None,
            "has_unknown_model_cost": data.cost_total["has_unknown_model_cost"]
            if data.cost_total
            else None,
        },
        "responses_by_effort": dict(sorted(efforts.items())),
        "by_model_and_effort": data.usage_by_model,
        "cost_by_model": data.cost_by_model,
    }


def build_readme(data: ChatData, folder_name: str, agent_files: int) -> str:
    """``README.md`` of a chat folder: what the chat is and what each file means."""
    chat = data.chat
    account = chat["account_label"] or chat["account_name"] or "unknown"
    project = chat["project_label"] or chat["project_name"] or "unknown"
    lines = [
        f"# {chat['title'] or 'Untitled chat'}",
        "",
        "One Claude Code chat: a conversation between a developer and Claude, with everything the",
        "index knows about it. This folder is derived from the raw data in `sessions/` and can be",
        "rebuilt at any time (`ccp-catalog build`); do not edit it by hand.",
        "",
        f"- Chat id: `{chat['chat_id']}`",
        f"- Folder: `{folder_name}`",
        f"- Claude account: {account}",
        f"- Project: {project}",
        f"- Started: {chat['started_at'].isoformat() if chat['started_at'] else 'unknown'}",
        f"- Sub-agent transcripts: {agent_files}",
        "",
        "## Files",
        "",
    ]
    lines += [f"- `{name}`: {meaning}" for name, meaning in FILES.items()]
    lines += [
        "",
        "In `transcript/`, each line of the original JSONL is one object of the array, in order.",
        'References such as `{"key": ..., "line": N}` in the JSON files point to line N of the raw',
        "object `key` in this bucket.",
        "",
    ]
    return "\n".join(lines)


TOP_README = """# CCP catalog

A readable view of the captured Claude Code sessions (account -> project -> session -> chat ->
conversations). It is **derived** from the raw data under
`sessions/` (which is never changed) and can be deleted and rebuilt at any time with
`ccp-catalog build --all`.

## Layout

```text
catalog/
  README.md                      this file
  index.json                     every account, project and chat, with its path
  accounts/<account>__<id8>/     one Claude account (its label or name, and 8 characters of its id)
    projects/<project>/          one project (its label or name)
      sessions/<date>__<title>__<id8>/ one Claude Code session: start date, title, id
        README.md                what the chat is and what each file means
        00_summary.json          ids, account, project, models, tokens, cost, counts
        01_conversation.md       the conversation, readable
        02_sessions.json         each time the chat was opened
        03_decisions.json        decisions and their review
        04_tests.json            test runs and test cases
        05_code_changes.json     file changes and linked commits
        06_corrections.json      what had to be put right
        07_subagents_and_workflows.json
        08_usage.json            tokens, effort, cost
        transcript/main.json     raw lines of the chat, as a JSON array
        transcript/agents/       raw lines of each sub-agent
```

Chats that were opened and closed without a message are not listed. When a title or a label
changes, the chat's folder moves; the id at the end of the folder name stays the same.
"""
