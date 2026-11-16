# ruff: noqa: E501  (table header lists and prose strings are clearer on one line)
"""The single readable report of a chat: everything the catalog knows, as one Markdown file.

``report.md`` holds the content of every other file of the chat folder (summary, openings,
conversation, decisions, tests, code changes, corrections, sub-agents, usage, README), turned
from JSON into headings, tables and lists. Nothing is cut: long tool inputs and outputs and
thinking notes are kept in full inside collapsible ``<details>`` blocks. A section with no data
says ``None recorded`` so that nothing disappears silently. The raw transcript is not copied
(it is the source; the report gives its object keys and line numbers).

The report is made from :class:`~ccp.catalog.loader.ChatData` only: no database, no object store.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from ccp.catalog import documents
from ccp.catalog.conversation import summarise_tool_call, text_of
from ccp.catalog.loader import ChatData

NONE = "*None recorded.*"
_HEADING = re.compile(r"^(#{1,6})(\s+\S.*)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_BASE_DEPTH = 4  # replies sit under "### Turn N"; Claude's own headings start at level 5


def fmt_time(value: Any) -> str:
    """A time as ``2026-11-16 05:08:03 UTC``; text and None are passed through."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return "—" if value is None else str(value)


def _clock(value: Any) -> str:
    return value.astimezone(UTC).strftime("%H:%M:%S") if isinstance(value, datetime) else "--:--:--"


def fmt_value(value: Any) -> str:
    """One value as plain inline text (used in tables and lists)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return fmt_time(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else str(value)
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return "; ".join(f"{k}: {fmt_value(v)}" for k, v in value.items()) or "—"
    if isinstance(value, list | tuple | set):
        return ", ".join(fmt_value(v) for v in value) or "—"
    return str(value)


def cell(value: Any) -> str:
    """A value made safe for a table cell: one line, pipes escaped."""
    return fmt_value(value).replace("|", "\\|").replace("\r", "").replace("\n", " ")


def table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    """A Markdown table (or the ``None recorded`` note when there are no rows)."""
    if not rows:
        return [NONE, ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return [*out, ""]


def facts(pairs: list[tuple[str, Any]]) -> list[str]:
    """A two-column ``Field | Value`` table."""
    return table(["Field", "Value"], [[k, v] for k, v in pairs])


def fence(text: str, lang: str = "") -> list[str]:
    """A fenced code block whose fence is longer than any backtick run inside the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    mark = "`" * max(3, longest + 1)
    return [f"{mark}{lang}", text.rstrip("\n"), mark, ""]


def quote(text: str) -> list[str]:
    """Text as a block quote (every line, so fenced code inside stays inside the quote)."""
    return [f"> {line}" if line else ">" for line in text.strip().splitlines()] + [""]


def demote(text: str) -> str:
    """Push Markdown headings down so a reply never outranks the turn it belongs to.

    ``#`` becomes level 5, ``##`` level 6, deeper headings stay at 6. Lines inside code fences
    are left alone.
    """
    out: list[str] = []
    fence_mark = ""
    for line in text.splitlines():
        found = _FENCE.match(line)
        if found:
            mark = found.group(1)
            if not fence_mark:
                fence_mark = mark[0]
            elif mark[0] == fence_mark:
                fence_mark = ""
        heading = None if fence_mark and not found else _HEADING.match(line)
        if heading and not found:
            level = min(6, len(heading.group(1)) + _BASE_DEPTH)
            line = "#" * level + heading.group(2)
        out.append(line)
    return "\n".join(out)


def details(summary: str, body: list[str]) -> list[str]:
    """A collapsible block; the blank lines let Markdown render what is inside."""
    return [f"<details><summary>{summary}</summary>", "", *body, "</details>", ""]


def ref_text(key: Any, line: Any = None) -> str:
    """A pointer into the raw data: ``key`` line N."""
    if not key:
        return "—"
    return f"`{key}` line {line}" if line is not None else f"`{key}`"


def _ref_of(value: Any) -> str:
    if isinstance(value, dict):
        return ref_text(value.get("key"), value.get("line"))
    return "—"


# ---------------------------------------------------------------- tool calls and results


def _input_lines(tool_input: Any) -> list[str]:
    """A tool's input in full: short values inline, long or multi-line values in code blocks."""
    if not isinstance(tool_input, dict):
        return fence(json.dumps(tool_input, indent=2, ensure_ascii=False, default=str), "json")
    out: list[str] = []
    for key, value in tool_input.items():
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        if "\n" in text or len(text) > 100:
            out += [f"**{key}:**", "", *fence(text)]
        else:
            out += [f"- **{key}:** `{text}`" if text else f"- **{key}:** (empty)"]
    return [*out, ""] if out else ["(no input)", ""]


def result_text(block: dict[str, Any]) -> str:
    """The text of a tool result block; images and other blocks are named, not dropped."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        elif isinstance(item, dict):
            parts.append(f"[{item.get('type', 'unknown')} block]")
    return "\n".join(parts)


class _Turn:
    """One developer prompt (or slash command) and everything that followed it."""

    def __init__(self, title: str, at: datetime | None, abandoned: bool) -> None:
        self.title, self.at, self.abandoned = title, at, abandoned
        self.body: list[str] = []
        self.actions = 0


def _in_window(moment: Any, start: Any, end: Any) -> bool:
    if not isinstance(moment, datetime) or not isinstance(start, datetime):
        return False
    return moment >= start and (end is None or moment < end)


def _conversation(data: ChatData) -> list[str]:
    """Section 3: the conversation, one turn per developer prompt."""
    slash = {row["event_id"]: row for row in data.slash_commands if "event_id" in row}
    turns: list[_Turn] = []
    calls: dict[str, tuple[int, str]] = {}  # tool_use id -> (action number, tool name)
    prompts = 0
    for event in data.events:
        kind, clock, content = event["event_type"], _clock(event.get("occurred_at")), None
        content = event.get("content")
        flag = " (rewound)" if event.get("is_abandoned") else ""
        if kind == "prompt" and event.get("actor") in ("human", "subagent"):
            prompts += 1
            who = (
                "Developer prompt"
                if event.get("actor") == "human"
                else "Instructions to the sub-agent"
            )
            turn = _Turn(
                f"Conversation {prompts} — {clock}{flag}", event.get("occurred_at"), bool(flag)
            )
            turn.body += [f"#### {who}", "", *quote(text_of(content))]
            turns.append(turn)
            continue
        if kind == "slash_command":
            row = slash.get(event.get("event_id"), {})
            command = f"{row.get('name') or '?'} {row.get('arguments') or ''}".strip()
            turns.append(_Turn(f"Command — {clock}{flag}", event.get("occurred_at"), bool(flag)))
            turns[-1].body += ["#### Slash command", "", *fence(command)]
            continue
        if not turns:
            turns.append(_Turn("Before the first prompt", event.get("occurred_at"), False))
        turn = turns[-1]
        if kind in ("response", "tool_call", "thinking"):
            _assistant(turn, content, clock, calls)
        elif kind == "tool_result":
            _results(turn, content, calls)
        elif kind == "compact_boundary":
            compact = event.get("compact")
            turn.body += ["#### Conversation compacted", "", *facts(list((compact or {}).items()))]
        elif kind in ("agent_message", "command_output"):
            label = (
                "Message from a sub-agent or background task"
                if kind == "agent_message"
                else "Command output"
            )
            turn.body += [f"#### {label} ({clock})", "", *fence(text_of(content))]
    if not turns:
        return [NONE, ""]
    out: list[str] = []
    for index, turn in enumerate(turns):
        end = turns[index + 1].at if index + 1 < len(turns) else None
        out += [f"### {turn.title}", "", *turn.body]
        changes = [c for c in data.file_changes if _in_window(c.get("occurred_at"), turn.at, end)]
        runs = [r for r in data.test_runs if _in_window(r.get("occurred_at"), turn.at, end)]
        if changes:
            out += ["#### Files changed in this turn", ""]
            out += table(
                ["File", "Change", "Tool", "Actor", "Lines +/-"],
                [[c["path"], c.get("change_type"), c.get("tool"), c.get("actor_class"),
                  f"+{c.get('lines_added') or 0}/-{c.get('lines_removed') or 0}"] for c in changes],
            )  # fmt: skip
        if runs:
            out += ["#### Test runs in this turn", ""]
            out += table(
                ["Command", "Passed", "Failed", "Skipped"],
                [[r.get("command"), r.get("tests_passed"), r.get("tests_failed"),
                  r.get("tests_skipped")] for r in runs],
            )  # fmt: skip
    return out


def _assistant(turn: _Turn, content: Any, clock: str, calls: dict[str, tuple[int, str]]) -> None:
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and str(block.get("text", "")).strip():
            turn.body += [f"**Claude ({clock}):**", "", demote(block["text"].strip()), ""]
        elif kind == "thinking" and block.get("thinking"):
            turn.body += details(f"Thinking ({clock})", quote(block["thinking"]))
        elif kind == "thinking":
            turn.body += [f"*Thinking ({clock}): the text was not recorded.*", ""]
        elif kind == "redacted_thinking":
            turn.body += [f"*Thinking ({clock}): redacted, not recorded.*", ""]
        elif kind == "tool_use":
            turn.actions += 1
            name = str(block.get("name", "?"))
            if block.get("id"):
                calls[str(block["id"])] = (turn.actions, name)
            label = summarise_tool_call(name, block.get("input")).replace("**", "")
            turn.body += details(
                f"Action {turn.actions} ({clock}): {label}".replace("<", "&lt;"),
                ["**Input**", "", *_input_lines(block.get("input"))],
            )


def _results(turn: _Turn, content: Any, calls: dict[str, tuple[int, str]]) -> None:
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        number, name = calls.get(str(block.get("tool_use_id")), (0, "?"))
        status = "error" if block.get("is_error") is True else "ok"
        text = result_text(block)
        title = (
            f"Result of action {number} ({name}): {status}" if number else f"Tool result: {status}"
        )
        turn.body += details(title, fence(text) if text.strip() else ["(empty result)", ""])


# ---------------------------------------------------------------- the other sections


def _summary(data: ChatData, folder: str) -> list[str]:
    s = documents.build_summary(data, folder)
    chat = data.chat
    git = (
        f"no Git repository (recorded: {chat.get('branch') or 'nothing'})"
        if (
            chat.get("branch") in (None, "HEAD")
            and not s["project"]["git_remote"]
            and not chat.get("start_commit")
        )
        else chat.get("branch")
    )
    helpers = _helper_models(data)
    models = [f"{m} (internal helper)" if m in helpers else m for m in s["models"]]
    started, ended = chat.get("started_at"), chat.get("ended_at")
    lasted = ""
    if isinstance(started, datetime) and isinstance(ended, datetime):
        lasted = str(ended - started).split(".")[0]
    out = facts([
        ("Chat id", f"`{s['chat_id']}`"), ("Title", s["title"]),
        ("Claude account", s["account"]["label"] or s["account"]["name"]),
        ("Account id", s["account"]["account_id"]), ("Account uuid", s["account"]["account_uuid"]),
        ("Project", s["project"]["label"] or s["project"]["name"]),
        ("Project id", s["project"]["project_id"]), ("Project key", s["project"]["key"]),
        ("Git remote", s["project"]["git_remote"] or "none"),
        ("Folder", s["folder"]), ("Git branch", git), ("Start commit", s["start_commit"]),
        ("Surface", s["surface"]), ("Status", s["status"]),
        ("Started", started), ("Last activity", s["last_activity_at"]),
        ("Last session end recorded", ended), ("Time between start and that end", lasted or None),
        ("Models", models), ("Cost (USD)", s["cost_usd"]), ("Catalog path", s["catalog_path"]),
    ])  # fmt: skip
    out += ["### Tokens", "", *facts(list(s["tokens"].items()))]
    out += ["### Counts", "", *facts(list(s["counts"].items()))]
    return out


def _helper_models(data: ChatData) -> set[str]:
    """Models that only appear in the cost record, not in any response of the chat."""
    worked = {r["model"] for r in data.usage_by_model if r.get("model")}
    return {r["model"] for r in data.cost_by_model if r.get("model") and r["model"] not in worked}


def _openings(data: ChatData) -> list[str]:
    cols = ["seq", "start_source", "started_at", "ended_at", "end_reason", "start_commit",
            "end_commit", "reported_by_client"]  # fmt: skip
    heads = ["#", "How opened", "Started", "Ended", "Why it ended", "Commit at start",
             "Commit at end", "Reported by the client"]  # fmt: skip
    return table(heads, [[r.get(c) for c in cols] for r in documents.build_sessions(data)])


def _decisions(data: ChatData) -> list[str]:
    out: list[str] = []
    items = documents.build_decisions(data)
    for d in items:
        r = d["review"]
        out += [f"### Decision {d['number']} — {d['title']}", ""]
        out += facts([
            ("Goal", d["goal"]), ("Chosen", d["chosen"]), ("Reason", d["reason"]),
            ("Result", d["result"]), ("Tests", d["tests"]), ("Decided by", d["decided_by"]),
            ("Written by (model)", d["written_by"]), ("Status", r["status"]),
            ("Checked by", r["checked_by"]), ("Reviewer", r["reviewer"]),
            ("Review note", r["note"]), ("Commits", d["commits"]),
            ("Decision id", d["decision_id"]), ("Created", d["created_at"]),
            ("Model usage", d["llm_usage"]),
        ])  # fmt: skip
        out += ["**Options considered**", ""]
        out += [f"- {o}" for o in d["options"]] + [""] if d["options"] else [NONE, ""]
        out += ["**Files changed by this decision**", ""]
        out += table(["File", "Change", "How"],
                     [[f["file"], f["change"], f["how"]] for f in d["files_changed"]])  # fmt: skip
        out += ["**Thinking notes**", ""]
        if d["thinking_notes"]:
            for t in d["thinking_notes"]:
                out += [
                    *quote(str(t["note"])),
                    f"Source: {ref_text(t['transcript_file'], t['line'])}",
                    "",
                ]
        else:
            out += [NONE, ""]
        out += ["**Review history**", ""]
        hist = r["history"] or []
        out += table(["When", "By kind", "Reviewer", "Status", "Note"],
                     [[h.get("at"), h.get("by_kind"), h.get("reviewer"), h.get("status"),
                       h.get("note")] for h in hist if isinstance(h, dict)])  # fmt: skip
        out += ["**Evidence in the raw transcript**", ""]
        out += (
            [f"- {ref_text(e['transcript_file'], e['line'])}" for e in d["evidence"]] + [""]
            if d["evidence"]
            else [NONE, ""]
        )
    return out or [NONE, ""]


def _code_changes(data: ChatData) -> list[str]:
    doc = documents.build_code_changes(data)
    out = [
        "### Summary",
        "",
        *facts([(k, v) for k, v in doc["summary"].items() if k != "changes_by_role"]),
    ]
    out += [
        "### Changes by file role",
        "",
        *table(["Role", "Changes"], [[k, v] for k, v in doc["summary"]["changes_by_role"].items()]),
    ]
    out += ["### Changes", ""]
    out += table(
        ["When", "File", "Change", "Tool", "Actor", "Role", "+", "-", "Tests added", "User modified", "Rewound", "Raw"],
        [[c.get("occurred_at"), c["path"], c.get("change_type"), c.get("tool"), c.get("actor_class"),
          c.get("file_role"), c.get("lines_added"), c.get("lines_removed"), c.get("tests_added"),
          c.get("user_modified"), c.get("is_abandoned"), _ref_of(c.get("raw"))]
         for c in doc["changes"]],
    )  # fmt: skip
    out += ["### Capture commits (every change, and who made it)", ""]
    out += table(
        ["When", "Who", "Developer", "Model", "Tool", "What", "Files", "Changed", "Commit", "Parent", "Chain", "Agent", "Prompt", "Prompt id", "Tool call", "#", "Event"],
        [[c.get("captured_at"), c.get("actor"), c.get("developer"), c.get("model"), c.get("tool"),
          c.get("target"), c.get("files_total"),
          ", ".join(f"{f.get('state')} {f.get('path')}" for f in (c.get("files") or [])
                    if isinstance(f, dict)),
          c.get("sha"), c.get("parent_sha"), c.get("chain"), c.get("agent"),
          c.get("prompt_excerpt"), c.get("prompt_uuid"), c.get("tool_use_id"), c.get("seq"),
          c.get("event")]
         for c in doc["capture_commits"]],
    )  # fmt: skip
    out += ["### Commits linked to this chat", ""]
    out += table(["Commit", "Method", "Confidence", "Matched lines", "Total lines", "Committed"],
                 [[c.get("commit_sha"), c.get("method"), c.get("confidence"), c.get("matched_lines"),
                   c.get("total_lines"), c.get("committed_at")] for c in doc["commits"]])  # fmt: skip
    return out


def _tests(data: ChatData) -> list[str]:
    doc = documents.build_tests(data)
    out = ["### Summary", "", *facts(list(doc["summary"].items())), "### Runs", ""]
    if not doc["runs"]:
        return [*out, NONE, ""]
    for n, run in enumerate(doc["runs"], 1):
        out += [f"#### Run {n} — {fmt_time(run.get('occurred_at'))}", ""]
        out += facts([
            ("Command", run.get("command")), ("Framework", run.get("framework")),
            ("Passed", run.get("passed")), ("Exit code", run.get("exit_code")),
            ("Tests passed", run.get("tests_passed")), ("Tests failed", run.get("tests_failed")),
            ("Tests skipped", run.get("tests_skipped")), ("Output source", run.get("output_source")),
            ("Raw", _ref_of(run.get("raw"))),
        ])  # fmt: skip
        out += table(["#", "Test case", "Outcome", "Message"],
                     [[c["position"], c["name"], c["outcome"], c["message"]] for c in run["cases"]])  # fmt: skip
    return out


def _corrections(data: ChatData) -> list[str]:
    return table(
        ["#", "Kind", "Corrected by", "Detected by", "Session", "Note", "Trigger at", "Trigger", "Fix at", "Fix"],
        [[k["correction_id"], k["kind"], k["corrected_by"], k["detected_by"], k["session_seq"], k["note"],
          k["trigger_at"], _ref_of(k["trigger"]), k["fix_at"], _ref_of(k["fix"])]
         for k in documents.build_corrections(data)],
    )  # fmt: skip


def _agents(data: ChatData) -> list[str]:
    out: list[str] = ["### Sub-agents", ""]
    if not data.agents:
        out += [NONE, ""]
    for a in data.agents:
        out += [f"#### {a.get('agent_type') or 'agent'} — `{a.get('agent_id')}`", ""]
        out += facts(
            [
                (k, v)
                for k, v in a.items()
                if k not in ("instructions", "result", "agent_id", "agent_type")
            ]
        )
        out += [
            "**Instructions**",
            "",
            *(quote(a["instructions"]) if a.get("instructions") else [NONE, ""]),
        ]
        out += ["**Result**", "", *(quote(a["result"]) if a.get("result") else [NONE, ""])]
        own = [e for e in data.agent_events if e.get("stream") == f"agent-{a.get('agent_id')}"]
        if own:  # everything the sub-agent wrote and ran, in full (its shell commands included)
            body = _conversation(ChatData(chat=data.chat, events=own))
            out += details(
                f"Full conversation of this sub-agent ({len(own)} entries)",
                [
                    line.replace("### Conversation", "##### Conversation", 1)
                    if line.startswith("### ")
                    else line
                    for line in body
                ],
            )
    out += ["### Workflow runs", ""]
    out += (
        table(list(data.workflows[0].keys()), [list(w.values()) for w in data.workflows])
        if data.workflows
        else [NONE, ""]
    )
    out += ["### Slash commands", ""]
    out += table(["When", "Rewound", "Command", "Arguments"],
                 [[s.get("occurred_at"), s.get("is_abandoned"), s.get("name"), s.get("arguments")]
                  for s in data.slash_commands])  # fmt: skip
    return out


def _usage(data: ChatData) -> list[str]:
    doc = documents.build_usage(data)
    total = doc["total"]
    out = facts([*total["tokens"].items(), ("Cost (USD)", total["cost_usd"]),
                 ("Some model has no known price", total["has_unknown_model_cost"])])  # fmt: skip
    out += [
        "### Responses by effort",
        "",
        *table(["Effort", "Responses"], [[k, v] for k, v in doc["responses_by_effort"].items()]),
    ]
    out += ["### Tokens by model and effort", ""]
    out += table(
        ["Model", "Effort", "Requested", "Responses", "Input", "Output", "Thinking", "Cache read", "Cache write", "Thinking ms"],
        [[r["model"], r["effort"], r["requested_model"], r["responses"], r["input_tokens"],
          r["output_tokens"], r["thinking_tokens"], r["cache_read_tokens"],
          r["cache_creation_tokens"], r["thinking_ms"]] for r in doc["by_model_and_effort"]],
    )  # fmt: skip
    helpers = _helper_models(data)
    out += ["### Cost by model (Claude Code's own cost record)", ""]
    out += table(
        ["Model", "Role", "Input", "Output", "Thinking", "Cache read", "Cache write", "Web searches", "Cost (USD)"],
        [[r["model"], "internal helper" if r["model"] in helpers else "worked on the chat",
          r["input_tokens"], r["output_tokens"], r["thinking_tokens"], r["cache_read_tokens"],
          r["cache_creation_tokens"], r["web_search_requests"], r["cost_usd"]] for r in doc["cost_by_model"]],
    )  # fmt: skip
    out += [
        "The first table is the sum of what each API response reported, so it is complete. The second "
        "is Claude Code's own cost record: a snapshot written at certain moments (for example when a "
        "session is closed), so it can miss the last replies, and it counts input tokens differently. "
        "The cost in the summary comes from that record, as of its last snapshot.",
        "",
    ]
    return out


def _about(data: ChatData, folder: str, agent_files: int) -> list[str]:
    out = [
        f"This report was generated from the index and the raw data of chat `{data.chat['chat_id']}`.",
        f"Catalog folder: `{folder}`. Raw transcript objects: `sessions/{data.chat['chat_id']}/` "
        f"(sub-agent transcripts: {agent_files}). References such as `key line N` point to line N of "
        "that raw object.",
        "",
        "Words used: account → project → **session** (one Claude Code session, one transcript) → "
        "**chat** (everything said in it) → **conversations** (one developer prompt and everything "
        "Claude did in answer). An **opening** is each time the session was started or resumed.",
        "",
        "Where each section's data also lives in the catalog folder:",
        "",
    ]
    mapping = [("1 Summary", "00_summary.json"), ("2 Openings", "02_sessions.json"),
               ("3 Conversation", "01_conversation.md"), ("4 Decisions", "03_decisions.json"),
               ("5 Code changes", "05_code_changes.json"), ("6 Tests", "04_tests.json"),
               ("7 Corrections", "06_corrections.json"),
               ("8 Sub-agents and workflows", "07_subagents_and_workflows.json"),
               ("9 Usage and cost", "08_usage.json"), ("10 About", "README.md")]  # fmt: skip
    out += table(["Section", "Catalog file"], [[a, f"`{b}`"] for a, b in mapping])
    return out


def render_report(data: ChatData, folder: str, agent_files: int = 0) -> str:
    """The whole report of a chat as Markdown.

    Args:
        data: Everything the catalog knows about the chat.
        folder: The chat's catalog folder in the bucket.
        agent_files: How many sub-agent transcripts the chat has.
    """
    chat = data.chat
    title = chat.get("title") or "Untitled chat"
    account = chat.get("account_label") or chat.get("account_name") or "unknown account"
    project = chat.get("project_label") or chat.get("project_name") or "unknown project"
    sections = [
        ("1. Summary", _summary(data, folder)),
        ("2. Openings of the session", _openings(data)),
        ("3. Chat: every conversation, in full", _conversation(data)),
        ("4. Decisions", _decisions(data)),
        ("5. Code changes", _code_changes(data)),
        ("6. Tests and checks", _tests(data)),
        ("7. Corrections", _corrections(data)),
        ("8. Sub-agents, workflows and slash commands", _agents(data)),
        ("9. Usage and cost", _usage(data)),
        ("10. About this report", _about(data, folder, agent_files)),
    ]
    out = [
        f"# {title}",
        "",
        f"> Account **{account}** → Project **{project}** → Session `{chat['chat_id']}` · "
        f"started {fmt_time(chat.get('started_at'))}",
        "",
        "## Contents",
        "",
    ]
    out += [f"- {name}" for name, _ in sections] + [""]
    for name, body in sections:
        out += [f"## {name}", "", *body]
    return "\n".join(out).rstrip() + "\n"
