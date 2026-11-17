# ruff: noqa: E501  (HTML, CSS and JavaScript are clearer on one line each)
"""The session report as a designed web page (``report.html``), to show to a client.

The page is made from the same data as ``report.md`` and holds the same content. The conversation
is laid out for reading: for each prompt, Claude's **thinking**, the **options** it weighed, the
**decision** (who made it: the developer or Claude, and with which model), what Claude then did
(every tool call and its output, shell commands included), and its answer. The other sections are the
tables of the Markdown report. Four templates (Studio, Paper, Midnight, Corporate) are in the one
file; a switch at the top changes between them and remembers the choice.

Nothing is loaded from the network (no CDN), so the file opens anywhere, offline. Every piece of text
is escaped, so the content of a session can never run as code in the page.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ccp.catalog import documents
from ccp.catalog.conversation import summarise_tool_call, text_of
from ccp.catalog.loader import ChatData
from ccp.catalog.report import fmt_time, fmt_value, render_report, result_text

THEMES = (
    ("studio", "Studio"),
    ("paper", "Paper"),
    ("midnight", "Midnight"),
    ("corporate", "Corporate"),
)
ACTOR_LABEL = {"human": "a person", "ai_edit": "Claude · edit tool", "ai_command": "Claude · shell command",
               "subagent": "sub-agent", "ai_other": "Claude"}  # fmt: skip


def esc(value: object) -> str:
    """HTML-escape any value (``None`` becomes an empty string)."""
    return "" if value is None else html.escape(str(value), quote=True)


# ---------------------------------------------------------------- a small, safe Markdown renderer

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_DETAILS = re.compile(r"^<details>(?:<summary>(.*)</summary>)?$")


def inline(text: str) -> str:
    """Inline Markdown (code, bold, italic, http links) on text that is escaped first."""
    out = esc(text)
    codes: list[str] = []

    def keep(match: re.Match[str]) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _INLINE_CODE.sub(keep, out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    out = _LINK.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', out)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)


def _cells(line: str) -> list[str]:
    parts = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [p.strip().replace("\\|", "|") for p in parts]


def markdown(text: str) -> str:
    """Render Markdown text (the subset the reports and Claude's replies use) as safe HTML."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            mark, body = fence.group(1), []
            i += 1
            while i < len(lines) and not (
                lines[i].strip().startswith(mark[0] * len(mark))
                and set(lines[i].strip()) <= {mark[0]}
            ):
                body.append(lines[i])
                i += 1
            i += 1
            out.append(f'<pre class="code"><code>{esc(chr(10).join(body))}</code></pre>')
            continue
        details = _DETAILS.match(line.strip())
        if details:
            summary = html.unescape(details.group(1) or "Details")
            out.append(f"<details><summary>{esc(summary)}</summary>")
            i += 1
            continue
        if line.strip() == "</details>":
            out.append("</details>")
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if heading:
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith(">"):
            quoted = []
            while i < len(lines) and lines[i].startswith(">"):
                quoted.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{markdown(chr(10).join(quoted))}</blockquote>")
            continue
        if (
            line.lstrip().startswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1])
        ):
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            table_body = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(
                '<div class="tablewrap"><table><thead><tr>'
                + "".join(f"<th>{inline(c)}</th>" for c in head)
                + f"</tr></thead><tbody>{table_body}</tbody></table></div>"
            )
            continue
        if re.match(r"^\s*([-*]|\d+[.)])\s+", line):
            ordered = bool(re.match(r"^\s*\d", line))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+[.)])\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*]|\d+[.)])\s+", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(t)}</li>" for t in items) + f"</{tag}>")
            continue
        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i])
            i += 1
        out.append(f"<p>{inline(' '.join(p.strip() for p in para))}</p>")
    return "\n".join(out)


def _starts_block(line: str) -> bool:
    return bool(
        _FENCE.match(line) or line.startswith(">") or line.lstrip().startswith("|")
        or re.match(r"^(#{1,6}\s|\s*([-*]|\d+[.)])\s+)", line) or _DETAILS.match(line.strip()) or line.strip() == "</details>"
    )  # fmt: skip


# ---------------------------------------------------------------- the conversation, structured


@dataclass
class Step:
    """One thing in a conversation: thinking, a reply, or a tool call with its output."""

    kind: str  # thinking | text | action | note
    clock: str
    text: str = ""
    number: int = 0
    tool: str = ""
    tool_input: Any = None
    result: str | None = None
    status: str = ""
    call_id: str = ""


@dataclass
class Turn:
    """One prompt (or slash command) and everything that followed it."""

    number: int
    title: str
    at: datetime | None
    start: tuple[str, int]
    abandoned: bool = False
    prompt: str = ""
    who: str = "Developer prompt"
    steps: list[Step] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    end: datetime | None = None


def _clock(value: Any) -> str:
    return value.astimezone(UTC).strftime("%H:%M:%S") if isinstance(value, datetime) else ""


def build_turns(
    events: list[dict[str, Any]], slash: dict[Any, dict[str, Any]] | None = None
) -> list[Turn]:
    """Group the main conversation's events into turns (one per developer prompt)."""
    slash = slash or {}
    turns: list[Turn] = []
    calls: dict[str, Step] = {}
    for event in events:
        kind, content, clock = (
            event["event_type"],
            event.get("content"),
            _clock(event.get("occurred_at")),
        )
        place = (str(event.get("raw_object_key") or ""), int(event.get("raw_line_number") or 0))
        flag = bool(event.get("is_abandoned"))
        if kind == "prompt" and event.get("actor") in ("human", "subagent"):
            who = (
                "Developer prompt"
                if event.get("actor") == "human"
                else "Instructions to the sub-agent"
            )
            turns.append(
                Turn(
                    len(turns) + 1,
                    f"Conversation {len(turns) + 1}",
                    event.get("occurred_at"),
                    place,
                    flag,
                    text_of(content),
                    who,
                )
            )
            continue
        if kind == "slash_command":
            row = slash.get(event.get("event_id"), {})
            command = f"{row.get('name') or '?'} {row.get('arguments') or ''}".strip()
            turns.append(
                Turn(
                    len(turns) + 1,
                    f"Command {len(turns) + 1}",
                    event.get("occurred_at"),
                    place,
                    flag,
                    command,
                    "Slash command",
                )
            )
            continue
        if not turns:
            turns.append(
                Turn(
                    1,
                    "Before the first prompt",
                    event.get("occurred_at"),
                    place,
                    False,
                    "",
                    "Session start",
                )
            )
        turn = turns[-1]
        turn.end = event.get("occurred_at") or turn.end
        if (
            event.get("model")
            and event["model"] != "<synthetic>"
            and event["model"] not in turn.models
        ):
            turn.models.append(str(event["model"]))
        if kind in ("response", "tool_call", "thinking"):
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and str(block.get("text", "")).strip():
                    turn.steps.append(Step("text", clock, str(block["text"]).strip()))
                elif btype == "thinking":
                    turn.steps.append(Step("thinking", clock, str(block.get("thinking") or "")))
                elif btype == "redacted_thinking":
                    turn.steps.append(
                        Step(
                            "thinking",
                            clock,
                            "(redacted: Claude Code did not record this reasoning)",
                        )
                    )
                elif btype == "tool_use":
                    step = Step("action", clock, number=sum(1 for s in turn.steps if s.kind == "action") + 1,
                                tool=str(block.get("name", "?")), tool_input=block.get("input"), call_id=str(block.get("id", "")))  # fmt: skip
                    turn.steps.append(step)
                    if step.call_id:
                        calls[step.call_id] = step
        elif kind == "tool_result":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    called = calls.get(str(block.get("tool_use_id")))
                    if called is not None:
                        called.result = result_text(block)
                        called.status = "error" if block.get("is_error") is True else "ok"
        elif kind == "compact_boundary":
            turn.steps.append(Step("note", clock, "The conversation was compacted here."))
        elif kind in ("agent_message", "command_output"):
            label = (
                "Message from a sub-agent or background task"
                if kind == "agent_message"
                else "Command output"
            )
            turn.steps.append(Step("note", clock, f"{label}:\n{text_of(content)}"))
    return turns


def assign_decisions(
    turns: list[Turn], decisions: list[dict[str, Any]]
) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Put each decision under the turn where its evidence starts; the rest are returned apart."""
    by_turn: dict[int, list[dict[str, Any]]] = {}
    loose: list[dict[str, Any]] = []
    for decision in decisions:
        places = [
            (str(e.get("transcript_file") or ""), int(e.get("line") or 0))
            for e in decision.get("evidence") or []
            if isinstance(e, dict)
        ]
        places += [
            (str(n.get("transcript_file") or ""), int(n.get("line") or 0))
            for n in decision.get("thinking_notes") or []
            if isinstance(n, dict)
        ]
        target = None
        if places:
            first = min(places)
            before = [t for t in turns if t.start <= first]
            target = before[-1] if before else turns[0] if turns else None
        if target is None:
            loose.append(decision)
        else:
            by_turn.setdefault(target.number, []).append(decision)
    return by_turn, loose


# ---------------------------------------------------------------- rendering pieces


def chip(text: object, kind: str = "") -> str:
    """A small coloured label."""
    return f'<span class="chip {esc(kind)}">{esc(text)}</span>'


def decider(decision: dict[str, Any], turn_models: list[str]) -> str:
    """Who decided: the developer, or Claude with the model that was acting."""
    who = decision.get("decided_by")
    if who == "developer":
        return chip("the developer decided", "human")
    model = ", ".join(turn_models) or "model not recorded"
    return (
        chip(f"Claude decided · {model}", "ai")
        if who == "claude"
        else chip(f"decided by {who or 'not stated'}", "other")
    )


def decision_card(decision: dict[str, Any], turn_models: list[str], full: bool = False) -> str:
    """A decision: the options, what was chosen, why, who decided and who reviewed it."""
    review = decision.get("review") or {}
    options = "".join(
        f'<li class="opt{" chosen" if _is_chosen(o, decision.get("chosen")) else ""}">{inline(str(o))}</li>'
        for o in decision.get("options") or []
    )
    parts = [
        f'<div class="decision" id="decision-{esc(decision.get("number"))}">',
        f'<div class="dhead"><span class="dtag">Decision {esc(decision.get("number"))}</span><strong>{esc(decision.get("title"))}</strong>'
        f"{decider(decision, turn_models)}{chip(review.get('status'), str(review.get('status')))}</div>",
        f'<p class="goal"><span>Goal</span>{inline(str(decision.get("goal") or "not stated"))}</p>',
    ]
    if options:
        parts.append(
            f'<div class="options"><span class="lab">Options considered</span><ul>{options}</ul></div>'
        )
    parts.append(
        f'<div class="chosen"><span class="lab">Chosen</span>{inline(str(decision.get("chosen") or "not stated"))}</div>'
    )
    parts.append(
        f'<p class="why"><span>Why</span>{inline(str(decision.get("reason") or "not stated"))}</p>'
    )
    if full:
        parts.append(
            f'<p><span class="lab">Result</span> {inline(str(decision.get("result") or "not stated"))}</p>'
        )
        parts.append(
            f'<p><span class="lab">Tests</span> {inline(str(decision.get("tests") or "not stated"))}</p>'
        )
        files = decision.get("files_changed") or []
        if files:
            parts.append(
                '<div class="tablewrap"><table><thead><tr><th>File</th><th>Change</th><th>How</th></tr></thead><tbody>'
                + "".join(
                    f"<tr><td><code>{esc(f.get('file'))}</code></td><td>{esc(f.get('change'))}</td><td>{esc(f.get('how'))}</td></tr>"
                    for f in files
                )
                + "</tbody></table></div>"
            )
        for note in decision.get("thinking_notes") or []:
            parts.append(
                f'<blockquote class="think"><span class="lab">Claude\'s thinking at the time</span>{esc(note.get("note"))}<small>{esc(note.get("transcript_file"))} line {esc(note.get("line"))}</small></blockquote>'
            )
        parts.append(
            '<p class="meta">'
            + " · ".join(
                [
                    f"written by {esc(decision.get('written_by'))}",
                    f"checked by {esc(review.get('checked_by'))} ({esc(review.get('reviewer'))})",
                    f"review note: {esc(review.get('note'))}",
                    f"commits: {esc(fmt_value(decision.get('commits')))}",
                    f"id {esc(decision.get('decision_id'))}",
                    f"created {esc(fmt_time(decision.get('created_at')))}",
                    f"model usage: {esc(fmt_value(decision.get('llm_usage')))}",
                ]
            )
            + "</p>"  # fmt: skip
        )
        history = review.get("history") or []
        if history:
            parts.append(
                '<details><summary>Review history</summary><div class="tablewrap"><table><thead><tr><th>When</th><th>By</th><th>Reviewer</th><th>Status</th><th>Note</th></tr></thead><tbody>'
                + "".join(
                    f"<tr><td>{esc(h.get('at'))}</td><td>{esc(h.get('by_kind'))}</td><td>{esc(h.get('reviewer'))}</td><td>{esc(h.get('status'))}</td><td>{esc(h.get('note'))}</td></tr>"
                    for h in history
                    if isinstance(h, dict)
                )
                + "</tbody></table></div></details>"
            )
        evidence = decision.get("evidence") or []
        if evidence:
            parts.append(
                "<details><summary>Evidence in the raw transcript</summary><ul>"
                + "".join(
                    f"<li><code>{esc(e.get('transcript_file'))}</code> line {esc(e.get('line'))}</li>"
                    for e in evidence
                )
                + "</ul></details>"
            )
    parts.append("</div>")
    return "".join(parts)


def _is_chosen(option: object, chosen: object) -> bool:
    name = str(option).split(":", 1)[0].strip().lower()
    return bool(name) and name in str(chosen or "").lower()


def _input_html(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return f'<pre class="code"><code>{esc(json.dumps(tool_input, indent=2, ensure_ascii=False, default=str))}</code></pre>'
    rows = []
    for key, value in tool_input.items():
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        )
        body = (
            f'<pre class="code"><code>{esc(text)}</code></pre>'
            if "\n" in text or len(text) > 100
            else f"<code>{esc(text)}</code>"
        )
        rows.append(f'<div class="kv"><span class="lab">{esc(key)}</span>{body}</div>')
    return "".join(rows) or "<em>no input</em>"


def _question_card(step: Step) -> str:
    """The exact options Claude asked a person to choose from, and the answer."""
    data = step.tool_input if isinstance(step.tool_input, dict) else {}
    cards = []
    for q in data.get("questions") or []:
        if not isinstance(q, dict):
            continue
        opts = "".join(
            f'<li class="opt"><strong>{esc(o.get("label"))}</strong><span>{esc(o.get("description"))}</span></li>'
            for o in q.get("options") or []
            if isinstance(o, dict)
        )
        cards.append(
            f'<div class="ask"><span class="lab">Claude asked{" (several allowed)" if q.get("multiSelect") else ""}</span><p>{esc(q.get("question"))}</p><ul class="optlist">{opts}</ul></div>'
        )
    answer = f'<div class="chosen"><span class="lab">The developer answered</span>{esc(step.result or "no answer recorded")}</div>'
    return f'<div class="action ask-card"><div class="alabel">Options put to the developer · {esc(step.clock)}</div>{"".join(cards)}{answer}</div>'


def step_html(step: Step) -> str:
    """One step of a conversation."""
    if step.kind == "thinking":
        body = (
            esc(step.text)
            if step.text.strip()
            else "<em>Claude Code stored no text for this reasoning step.</em>"
        )
        return f'<div class="think"><span class="lab">Claude\'s thinking · {esc(step.clock)}</span><div class="pre">{body}</div></div>'
    if step.kind == "text":
        return f'<div class="reply"><span class="lab">Claude · {esc(step.clock)}</span>{markdown(step.text)}</div>'
    if step.kind == "note":
        return f'<div class="note"><pre>{esc(step.text)}</pre></div>'
    if step.tool == "AskUserQuestion":
        return _question_card(step)
    summary = summarise_tool_call(step.tool, step.tool_input).replace("**", "")
    status = chip(
        step.status or "no result",
        "bad" if step.status == "error" else "ok" if step.status == "ok" else "other",
    )
    extra = ""
    if (
        step.tool == "ExitPlanMode"
        and isinstance(step.tool_input, dict)
        and step.tool_input.get("plan")
    ):
        extra = f'<div class="plan"><span class="lab">Plan put to the developer for approval</span>{markdown(str(step.tool_input["plan"]))}</div>'
    output = (
        f'<div class="kv"><span class="lab">Output</span><pre class="code out"><code>{esc(step.result)}</code></pre></div>'
        if step.result
        else '<div class="kv"><em>no output recorded</em></div>'
    )
    return (
        f'<details class="action"><summary><span class="tool">{esc(step.tool)}</span> <code>{esc(summary.split(" ", 1)[1] if " " in summary else "")}</code> {status}'
        f'<small>#{step.number} · {esc(step.clock)}</small></summary>{extra}<div class="kv"><span class="lab">Input</span></div>{_input_html(step.tool_input)}{output}</details>'
    )


def turn_html(
    turn: Turn, decisions: list[dict[str, Any]], data: ChatData, next_at: datetime | None
) -> str:
    """One conversation: prompt, thinking, options, decision, actions and answer."""

    def within(moment: Any) -> bool:
        return (
            isinstance(moment, datetime)
            and isinstance(turn.at, datetime)
            and moment >= turn.at
            and (next_at is None or moment < next_at)
        )

    head = (
        f'<div class="thead"><span class="num">{turn.number}</span><div><strong>{esc(turn.title)}</strong><small>{esc(fmt_time(turn.at))}{" · rewound" if turn.abandoned else ""}</small></div>'
        + "".join(chip(m, "ai") for m in turn.models)
        + "</div>"
    )
    prompt = (
        f'<div class="prompt"><span class="lab">{esc(turn.who)}</span>{markdown(turn.prompt) if turn.prompt else "<em>empty</em>"}</div>'
        if turn.prompt or turn.who != "Developer prompt"
        else ""
    )
    first_think = [s for s in turn.steps if s.kind == "thinking"]
    flow = "".join(step_html(s) for s in turn.steps)
    decided = "".join(decision_card(d, turn.models) for d in decisions)
    changes = [c for c in data.file_changes if within(c.get("occurred_at"))]
    runs = [r for r in data.test_runs if within(r.get("occurred_at"))]
    extras = ""
    if changes:
        extras += (
            '<div class="extra"><span class="lab">Files changed in this conversation</span><div class="tablewrap"><table><thead><tr><th>File</th><th>Change</th><th>Tool</th><th>Who</th><th>Lines</th></tr></thead><tbody>'
            + "".join(
                f"<tr><td><code>{esc(c['path'])}</code></td><td>{esc(c.get('change_type'))}</td><td>{esc(c.get('tool'))}</td><td>{chip(c.get('actor_class'), str(c.get('actor_class')))}</td><td>+{esc(c.get('lines_added') or 0)} / -{esc(c.get('lines_removed') or 0)}</td></tr>"
                for c in changes
            )
            + "</tbody></table></div></div>"
        )
    if runs:
        extras += (
            '<div class="extra"><span class="lab">Tests and checks run in this conversation</span><div class="tablewrap"><table><thead><tr><th>Command</th><th>Result</th><th>Passed</th><th>Failed</th></tr></thead><tbody>'
            + "".join(
                f"<tr><td><code>{esc(str(r.get('command'))[:140])}</code></td><td>{chip('passed' if r.get('passed') else 'failed', 'passed' if r.get('passed') else 'failed')}</td><td>{esc(r.get('tests_passed'))}</td><td>{esc(r.get('tests_failed'))}</td></tr>"
                for r in runs
            )
            + "</tbody></table></div></div>"
        )
    summary = ""
    if first_think or decisions:
        bits = [
            f"{len(first_think)} thinking step(s)" if first_think else "",
            f"{len(decisions)} decision(s)" if decisions else "",
            f"{sum(1 for s in turn.steps if s.kind == 'action')} action(s)",
        ]
        summary = f'<div class="tsummary">{" · ".join(b for b in bits if b)}</div>'
    return f'<article class="turn{" rewound" if turn.abandoned else ""}" id="conversation-{turn.number}">{head}{prompt}{summary}{flow}{decided}{extras}</article>'


# ---------------------------------------------------------------- the page


_CSS = """
:root,[data-theme=studio]{--bg:#f4f5fb;--card:#fff;--ink:#1e2433;--muted:#667085;--line:#e4e7ee;--brand:#5b4fe8;--brand2:#0ea5e9;--soft:rgba(91,79,232,.08);--think:#fff7e6;--thinkb:#f5c26b;--ok:#16a34a;--bad:#dc2626;--warn:#d97706;--human:#16a34a;--ai:#5b4fe8;--code:#0f172a;--codei:#e2e8f0;--radius:16px;--font:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;--hero:linear-gradient(120deg,#5b4fe8,#0ea5e9)}
[data-theme=paper]{--bg:#fbfaf7;--card:#fff;--ink:#222;--muted:#6b6b6b;--line:#e2ded4;--brand:#8a3b12;--brand2:#8a3b12;--soft:rgba(138,59,18,.07);--think:#fbf6e9;--thinkb:#d8c38a;--code:#f3efe6;--codei:#2b2b2b;--radius:4px;--font:Georgia,"Times New Roman",serif;--hero:#fff}
[data-theme=midnight]{--bg:#0b1020;--card:#131a2e;--ink:#e6e9f2;--muted:#8f9bb8;--line:#25304d;--brand:#7c8cff;--brand2:#22d3ee;--soft:rgba(124,140,255,.12);--think:#2a2310;--thinkb:#a67c1f;--ok:#34d399;--bad:#f87171;--warn:#fbbf24;--human:#34d399;--ai:#7c8cff;--code:#070b17;--codei:#cbd5e1;--radius:14px;--hero:linear-gradient(120deg,#1e293b,#312e81)}
[data-theme=corporate]{--bg:#eef2f6;--card:#fff;--ink:#14213d;--muted:#5b6b82;--line:#d5dde8;--brand:#0b5cad;--brand2:#0b5cad;--soft:rgba(11,92,173,.07);--think:#eef5fb;--thinkb:#8fb6dc;--code:#14213d;--codei:#e8eef6;--radius:6px;--hero:#0b3d6e}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 var(--font)}
.hero{background:var(--hero);color:#fff;padding:34px 40px 26px}[data-theme=paper] .hero{color:var(--ink);border-bottom:3px double var(--line)}
.hero .crumbs{opacity:.85;font-size:13px;letter-spacing:.02em}.hero h1{margin:6px 0 14px;font-size:30px;line-height:1.2}
.stats{display:flex;flex-wrap:wrap;gap:10px}.stat{background:rgba(255,255,255,.16);border-radius:12px;padding:8px 14px;min-width:96px}
[data-theme=paper] .stat{background:var(--soft)}.stat b{display:block;font-size:20px}.stat small{opacity:.85;font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.bar{position:sticky;top:0;z-index:5;display:flex;gap:10px;align-items:center;justify-content:space-between;background:var(--card);border-bottom:1px solid var(--line);padding:8px 20px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.bar nav{display:flex;gap:4px;flex-wrap:wrap}.bar nav a{color:var(--brand);text-decoration:none;font-size:13px;padding:5px 11px;border-radius:999px}.bar nav a:hover{background:var(--soft)}
.tools{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)}.tools button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:4px 10px;cursor:pointer;font:inherit}
.tools button.on{background:var(--brand);color:#fff;border-color:var(--brand)}
main{max-width:1160px;margin:26px auto;padding:0 18px}section.sec{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:6px 26px 22px;margin:0 0 22px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
section.sec>h2{color:var(--brand);border-bottom:2px solid var(--line);padding-bottom:8px;margin:18px 0 14px;font-size:21px}
h3{margin:22px 0 8px;padding:5px 10px;border-left:4px solid var(--brand2);background:var(--soft);border-radius:4px;font-size:16px}h4,h5,h6{color:var(--muted);margin:14px 0 6px}
.tablewrap{overflow-x:auto;margin:10px 0}table{border-collapse:collapse;width:100%;font-size:13.5px}th{background:var(--soft);text-align:left;color:var(--brand)}th,td{border:1px solid var(--line);padding:7px 10px;vertical-align:top}tbody tr:nth-child(even) td{background:rgba(127,127,127,.045)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.92em}p code,td code,li code{background:var(--soft);padding:1px 5px;border-radius:5px}
pre.code{background:var(--code);color:var(--codei);padding:12px 14px;border-radius:10px;overflow:auto;max-height:420px;font-size:12.5px;margin:6px 0}pre.code code{background:none;padding:0}
blockquote{margin:10px 0;padding:6px 16px;border-left:4px solid var(--brand);background:var(--soft);border-radius:6px}
details{border:1px solid var(--line);border-radius:10px;padding:6px 14px;margin:8px 0}summary{cursor:pointer;font-weight:600}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600;color:#fff;background:var(--muted);margin-left:6px;vertical-align:middle}
.chip.ai,.chip.ai_edit{background:var(--ai)}.chip.human{background:var(--human)}.chip.ai_command{background:var(--warn)}.chip.subagent{background:#db2777}.chip.ok,.chip.approved,.chip.passed{background:var(--ok)}.chip.bad,.chip.rejected,.chip.failed,.chip.error{background:var(--bad)}.chip.needs_review{background:var(--warn)}
.turn{border:1px solid var(--line);border-radius:var(--radius);margin:22px 0;overflow:hidden;background:var(--card)}.turn.rewound{opacity:.7;border-style:dashed}
.thead{display:flex;gap:14px;align-items:center;padding:12px 18px;background:var(--soft);border-bottom:1px solid var(--line)}.thead .num{background:var(--brand);color:#fff;border-radius:50%;width:34px;height:34px;display:grid;place-items:center;font-weight:700}.thead small{display:block;color:var(--muted)}
.turn>*:not(.thead){margin-left:18px;margin-right:18px}.turn>*:last-child{margin-bottom:18px}
.lab{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:3px}
.prompt{margin-top:16px;background:linear-gradient(0deg,var(--soft),var(--soft));border-left:5px solid var(--human);border-radius:10px;padding:10px 16px}.prompt p{margin:.4em 0}
.tsummary{margin-top:10px;color:var(--muted);font-size:12.5px}
.think{margin-top:12px;background:var(--think);border:1px solid var(--thinkb);border-radius:10px;padding:10px 16px}.think .pre{white-space:pre-wrap;font-style:italic}
.reply{margin-top:12px;border-left:5px solid var(--ai);padding:4px 16px;background:rgba(127,127,127,.035);border-radius:0 10px 10px 0}.reply h2,.reply h3,.reply h4,.reply h5,.reply h6{border:none;background:none;padding:0;color:var(--brand);font-size:16px}
.action{margin:8px 18px}.action summary{font-weight:500}.action .tool{font-weight:700;color:var(--brand)}.action small{float:right;color:var(--muted)}.kv{margin:6px 0}
.decision{margin-top:14px;border:2px solid var(--brand);border-radius:14px;padding:12px 18px 14px;background:var(--card)}.dhead{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}.dtag{background:var(--brand);color:#fff;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:700}
.options ul,.optlist{list-style:none;padding:0;margin:4px 0;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px}.opt{border:1px solid var(--line);border-radius:10px;padding:8px 12px;background:rgba(127,127,127,.04)}.opt span{display:block;color:var(--muted);font-size:13px}
.opt.chosen{border:2px solid var(--ok);background:rgba(22,163,74,.08)}.opt.chosen::before{content:"chosen  ";color:var(--ok);font-weight:700;font-size:11px;text-transform:uppercase}
.chosen{margin:8px 0;padding:8px 14px;border-left:5px solid var(--ok);background:rgba(22,163,74,.07);border-radius:0 10px 10px 0}.goal span,.why span{font-weight:700;color:var(--muted);margin-right:8px;text-transform:uppercase;font-size:11px;letter-spacing:.06em}
.ask-card{border:2px solid var(--warn);border-radius:12px;padding:10px 16px}.alabel{font-weight:700;color:var(--warn);margin-bottom:6px}.ask p{margin:2px 0 6px;font-weight:600}
.plan{border:1px dashed var(--brand);border-radius:10px;padding:8px 14px;margin:8px 0}.meta{font-size:12.5px;color:var(--muted)}.note pre{background:var(--soft);padding:8px 12px;border-radius:8px;white-space:pre-wrap}
.extra{margin-top:12px}footer{text-align:center;color:var(--muted);font-size:12px;padding:26px}
@media print{.bar{display:none}details{display:block}details>*{display:block}.hero{color:#000;background:#fff}}
@media (max-width:700px){.hero{padding:22px 16px}main{padding:0 8px}section.sec{padding:4px 12px 14px}}
"""

_SCRIPT = """
(function(){
  var root=document.documentElement, saved=null;
  try{saved=localStorage.getItem('ccp-theme');}catch(e){}
  function setTheme(t){root.setAttribute('data-theme',t);try{localStorage.setItem('ccp-theme',t);}catch(e){}
    document.querySelectorAll('.tools [data-theme-btn]').forEach(function(b){b.classList.toggle('on',b.dataset.themeBtn===t);});}
  document.querySelectorAll('.tools [data-theme-btn]').forEach(function(b){b.addEventListener('click',function(){setTheme(b.dataset.themeBtn);});});
  setTheme(saved||'studio');
  function all(open){document.querySelectorAll('main details').forEach(function(d){d.open=open;});}
  document.getElementById('expand').addEventListener('click',function(){all(true);});
  document.getElementById('collapse').addEventListener('click',function(){all(false);});
})();
"""


def _sections(md: str) -> list[tuple[str, str]]:
    """The numbered sections of the Markdown report: ``(title, body)``."""
    parts = re.split(r"(?m)^## (.+)$", md)
    return [
        (parts[i].strip(), parts[i + 1].strip("\n"))
        for i in range(1, len(parts) - 1, 2)
        if parts[i].strip() != "Contents"
    ]


def _stats(data: ChatData) -> str:
    summary = documents.build_summary(data, "")
    counts, tokens = summary["counts"], summary["tokens"]
    cost = summary.get("cost_usd")
    items = [
        ("Conversations", counts["prompts"]), ("Tool calls", counts["tool_calls"]), ("Decisions", counts["decisions"]),
        ("Files changed", counts["files_changed"]), ("Test runs", counts["test_runs"]),
        ("Tokens", f"{tokens['total']:,}"), ("Cost", f"${float(cost):.2f}" if cost is not None else "n/a"),
        ("Models", len(summary["models"])), ("Openings", counts["sessions"]),
    ]  # fmt: skip
    return "".join(
        f'<div class="stat"><b>{esc(v)}</b><small>{esc(k)}</small></div>' for k, v in items
    )


def render_report_html(
    data: ChatData, folder: str, agent_files: int = 0, raw_lines: list[Any] | None = None
) -> str:
    """The complete page for a session (same content as :func:`~ccp.catalog.report.render_report`)."""
    chat = data.chat
    title = chat.get("title") or "Claude Code session"
    account = chat.get("account_label") or chat.get("account_name") or "unknown account"
    project = chat.get("project_label") or chat.get("project_name") or "unknown project"
    md = render_report(data, folder, agent_files, raw_lines)
    decisions = documents.build_decisions(data)
    slash = {row["event_id"]: row for row in data.slash_commands if "event_id" in row}
    turns = build_turns(data.events, slash)
    by_turn, loose = assign_decisions(turns, decisions)

    conversation = (
        "".join(
            turn_html(
                t, by_turn.get(t.number, []), data, turns[i + 1].at if i + 1 < len(turns) else None
            )
            for i, t in enumerate(turns)
        )
        or "<p><em>None recorded.</em></p>"
    )
    all_decisions = (
        "".join(decision_card(d, _models_of(d, turns, by_turn), full=True) for d in decisions)
        or "<p><em>None recorded.</em></p>"
    )
    overview = (
        '<div class="tablewrap"><table><thead><tr><th>#</th><th>Decision</th><th>Decided by</th><th>Reviewed</th><th>Conversation</th></tr></thead><tbody>'
        + "".join(
            f'<tr><td>{esc(d["number"])}</td><td><a href="#decision-{esc(d["number"])}">{esc(d["title"])}</a></td><td>{decider(d, _models_of(d, turns, by_turn))}</td>'
            f"<td>{chip(d['review']['status'], str(d['review']['status']))} {esc(d['review']['reviewer'])}</td><td>{_turn_link(d, by_turn)}</td></tr>"
            for d in decisions
        )
        + "</tbody></table></div>"
        if decisions
        else ""
    )

    nav, body = [], []
    for number, (name, section) in enumerate(_sections(md), start=1):
        sid = f"s{number}"
        label = re.sub(r"^\d+\.\s*", "", name)
        nav.append(f'<a href="#{sid}">{esc(label)}</a>')
        if name.startswith("3."):
            inner = conversation
        elif name.startswith("4."):
            inner = overview + all_decisions
        elif name.startswith("1."):
            inner = markdown(section) + (
                "<h3>Decisions at a glance</h3>" + overview if overview else ""
            )
        else:
            inner = markdown(section)
        body.append(f'<section class="sec" id="{sid}"><h2>{esc(name)}</h2>{inner}</section>')
    if loose:
        body.append("")  # decisions without evidence lines are listed in section 4 only
    theme_buttons = "".join(f'<button data-theme-btn="{k}">{esc(v)}</button>' for k, v in THEMES)
    return (
        f'<!doctype html><html lang="en" data-theme="studio"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{_CSS}</style></head><body>"
        f'<header class="hero"><div class="crumbs">Account <b>{esc(account)}</b> &rarr; Project <b>{esc(project)}</b> &rarr; Session <code>{esc(chat["chat_id"])}</code> &middot; started {esc(fmt_time(chat.get("started_at")))}</div>'
        f'<h1>{esc(title)}</h1><div class="stats">{_stats(data)}</div></header>'
        f'<div class="bar"><nav>{"".join(nav)}</nav><div class="tools"><span>Template</span>{theme_buttons}'
        '<button id="expand">Expand all</button><button id="collapse">Collapse all</button></div></div>'
        f"<main>{''.join(body)}</main>"
        "<footer>Generated by the Claude Capture Platform from the recorded session. The same content is in report.md.</footer>"
        f"<script>{_SCRIPT}</script></body></html>"
    )


def _turn_of(decision: dict[str, Any], by_turn: dict[int, list[dict[str, Any]]]) -> int | None:
    for number, items in by_turn.items():
        if any(d.get("number") == decision.get("number") for d in items):
            return number
    return None


def _models_of(
    decision: dict[str, Any], turns: list[Turn], by_turn: dict[int, list[dict[str, Any]]]
) -> list[str]:
    number = _turn_of(decision, by_turn)
    return next((t.models for t in turns if t.number == number), [])


def _turn_link(decision: dict[str, Any], by_turn: dict[int, list[dict[str, Any]]]) -> str:
    number = _turn_of(decision, by_turn)
    return f'<a href="#conversation-{number}">Conversation {number}</a>' if number else "—"
