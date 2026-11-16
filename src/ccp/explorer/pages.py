# ruff: noqa: E501
"""The explorer website's HTML: small template functions built on :func:`html.escape`.

No template engine is used. Every value that comes from the database or the user goes through
:func:`esc` (or a helper that calls it) before it reaches the page, so the transcript text cannot
inject markup. Links are built from ``View.base`` (the path the site is mounted at, for example
``/explorer``) so the site works under any prefix.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from ccp.explorer.queries import CONVERSATION_PAGE_SIZE, Row, as_json_list
from ccp.explorer.users import ROLES, UiUser

# One static style sheet: light and dark (follows the browser), no framework, no script.
CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2430; --muted:#5d6879; --line:#dde1e8; --accent:#2457c5;
        --ok:#1a7f3c; --bad:#b3261e; --warn:#8a5a00; --code:#eef0f4; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#14171c; --card:#1c2027; --text:#e6e9ef; --muted:#9aa4b5; --line:#2c323c;
          --accent:#7aa2ff; --ok:#5fd183; --bad:#ff8a80; --warn:#f0b955; --code:#252a33; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
header.top { background:var(--card); border-bottom:1px solid var(--line); padding:.6rem 1.2rem;
             display:flex; flex-wrap:wrap; gap:1rem; align-items:center; }
header.top a { color:var(--text); text-decoration:none; font-weight:500; }
header.top a.brand { font-weight:700; color:var(--accent); }
header.top .who { margin-left:auto; color:var(--muted); display:flex; gap:.6rem; align-items:center; }
main { max-width:1200px; margin:0 auto; padding:1rem 1.2rem 3rem; }
h1 { font-size:1.4rem; margin:.4rem 0 1rem; } h2 { font-size:1.1rem; margin:1.4rem 0 .5rem; }
a { color:var(--accent); }
table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line);
        border-radius:6px; overflow:hidden; font-size:14px; }
th, td { text-align:left; padding:.4rem .6rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.card { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:.8rem 1rem;
        margin:.6rem 0; }
.muted { color:var(--muted); } .ok { color:var(--ok); } .bad { color:var(--bad); }
.badge { display:inline-block; padding:0 .5rem; border-radius:999px; font-size:12px;
         border:1px solid var(--line); background:var(--code); }
.badge.approved, .badge.passed { color:var(--ok); } .badge.rejected, .badge.failed, .badge.error { color:var(--bad); }
.badge.needs_review, .badge.draft { color:var(--warn); }
nav.tabs { display:flex; flex-wrap:wrap; gap:.3rem; margin:.8rem 0; border-bottom:1px solid var(--line); }
nav.tabs a { padding:.4rem .8rem; text-decoration:none; color:var(--muted); border-radius:6px 6px 0 0; }
nav.tabs a.on { color:var(--text); background:var(--card); border:1px solid var(--line);
                border-bottom-color:var(--card); font-weight:600; }
.msg { margin:.5rem 0; padding:.5rem .8rem; border-radius:6px; background:var(--card);
       border:1px solid var(--line); white-space:pre-wrap; word-break:break-word; }
.msg.prompt { border-left:4px solid var(--accent); } .msg.response { border-left:4px solid var(--ok); }
.msg.tool { background:transparent; border-style:dashed; color:var(--muted); font-size:13px; }
.msg.abandoned { opacity:.55; }
.msg .meta { font-size:12px; color:var(--muted); margin-bottom:.2rem; }
code, pre { background:var(--code); border-radius:4px; font-family:ui-monospace,Consolas,monospace; font-size:13px; }
code { padding:0 .25rem; } pre { padding:.5rem; overflow:auto; white-space:pre-wrap; }
form.inline { display:inline; } label { display:block; margin:.4rem 0 .15rem; color:var(--muted); font-size:13px; }
input, select, textarea { font:inherit; padding:.35rem .5rem; border:1px solid var(--line); border-radius:6px;
                          background:var(--card); color:var(--text); max-width:100%; }
textarea { width:100%; min-height:3.5rem; }
button { font:inherit; padding:.35rem .8rem; border:1px solid var(--line); border-radius:6px;
         background:var(--card); color:var(--text); cursor:pointer; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.login { max-width:380px; margin:4rem auto; }
.flash { background:var(--code); border:1px solid var(--line); border-radius:6px; padding:.5rem .8rem; margin:.6rem 0; }
.flash.error { border-color:var(--bad); }
.cols { display:flex; flex-wrap:wrap; gap:1rem; } .cols > * { flex:1 1 320px; }
details summary { cursor:pointer; color:var(--muted); }
@media (max-width:700px) { table { display:block; overflow-x:auto; } }
"""

# The tabs of a chat page: (key in the address, title).
TABS = (
    ("conversation", "Conversation"),
    ("sessions", "Sessions"),
    ("decisions", "Decisions"),
    ("tests", "Tests"),
    ("code", "Code changes"),
    ("corrections", "Corrections"),
    ("agents", "Sub-agents and workflows"),
    ("thinking", "Thinking notes"),
    ("usage", "Usage"),
)

_TEXT_LIMIT = 4000
_SUMMARY_LIMIT = 160


@dataclass(frozen=True)
class View:
    """What every page needs to know about the visitor and where the site is mounted.

    Attributes:
        base: The mount path without a trailing slash (``""`` or ``"/explorer"``).
        user: The logged-in user, or ``None`` (login page).
        csrf: The form token of the visitor's session.
    """

    base: str
    user: UiUser | None
    csrf: str

    def url(self, path: str, **query: object) -> str:
        """An address inside the site, with an optional query string (``None`` values skipped)."""
        pairs = {k: str(v) for k, v in query.items() if v is not None}
        return html.escape(self.base + path + (f"?{urlencode(pairs)}" if pairs else ""))


def esc(value: object) -> str:
    """Escape a value for HTML (``None`` becomes an empty string)."""
    return "" if value is None else html.escape(str(value), quote=True)


def when(value: object) -> str:
    """A time as ``YYYY-MM-DD HH:MM`` (UTC), or a dash."""
    return value.strftime("%Y-%m-%d %H:%M") if isinstance(value, datetime) else "-"


def num(value: object) -> str:
    """A whole number with thousands separators, or a dash."""
    return f"{int(value):,}" if isinstance(value, int | float) and value is not None else "-"


def short(text: object, limit: int = _SUMMARY_LIMIT) -> str:
    """The first line-wrapped ``limit`` characters of a text, with an ellipsis when cut."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def account_title(row: Row, privileged: bool) -> str:
    """The name of a Claude account: its label, else the e-mail (privileged) or a neutral name."""
    if row.get("account_label"):
        return str(row["account_label"])
    email = row.get("account_email")
    if privileged and (row.get("account_name") or email):
        return str(row.get("account_name") or email)
    if email:
        return (
            "account-"
            + hashlib.md5(str(email).lower().encode(), usedforsecurity=False).hexdigest()[:6]
        )
    return "unknown account"


def project_title(row: Row) -> str:
    """The name of a project: its label, else its name, else the last part of its folder."""
    if row.get("project_label"):
        return str(row["project_label"])
    if row.get("project_name"):
        return str(row["project_name"])
    tail = str(row.get("folder") or "").rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
    return tail or "unknown project"


def chat_title(row: Row) -> str:
    """A chat's title, or a short form of its id when it has none."""
    return str(row.get("title") or "") or f"(untitled {str(row['chat_id'])[:8]})"


# ---------------------------------------------------------------- the frame


def layout(view: View, title: str, body: str, flash: str = "", error: bool = False) -> str:
    """Wrap a page body in the site frame (header, navigation, style sheet)."""
    user = view.user
    nav = ""
    if user is not None:
        links = [f'<a href="{view.url("/")}">Home</a>']
        if user.is_admin:
            links.append(f'<a href="{view.url("/admin/users")}">Users</a>')
            links.append(f'<a href="{view.url("/admin/labels")}">Labels</a>')
        if user.reads_access_log:
            links.append(f'<a href="{view.url("/access-log")}">Access log</a>')
        who = f"<span>{esc(user.email)} ({esc(user.role)})</span>" + post_form(
            view, "/logout", '<button type="submit">Log out</button>', "inline"
        )
        nav = (
            '<header class="top"><a class="brand" href="'
            + view.url("/")
            + '">CCP Explorer</a>'
            + "".join(links)
            + f'<span class="who">{who}</span></header>'
        )
    notice = f'<div class="flash{" error" if error else ""}">{esc(flash)}</div>' if flash else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)} - CCP Explorer</title>"
        f'<link rel="stylesheet" href="{view.url("/style.css")}"></head><body>'
        f"{nav}<main>{notice}{body}</main></body></html>"
    )


def post_form(view: View, path: str, inner: str, css: str = "", **query: object) -> str:
    """A POST form to ``path`` with the CSRF token as its first field."""
    return (
        f'<form method="post" action="{view.url(path, **query)}" class="{css}">'
        f'<input type="hidden" name="csrf" value="{esc(view.csrf)}">{inner}</form>'
    )


def table(headers: list[str], rows: list[list[str]], numeric: set[int] | None = None) -> str:
    """A table. Cells are already HTML; ``numeric`` columns are right-aligned."""
    numeric = numeric or set()

    def cell(tag: str, index: int, content: str) -> str:
        return f"<{tag}{' class=num' if index in numeric else ''}>{content}</{tag}>"

    head = "".join(cell("th", i, esc(h)) for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(cell("td", i, c) for i, c in enumerate(r)) + "</tr>" for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def badge(text: object) -> str:
    """A small label coloured by its value (passed, failed, approved ...)."""
    return f'<span class="badge {esc(str(text).replace(" ", "_"))}">{esc(text)}</span>'


# ---------------------------------------------------------------- login, errors


def login_page(view: View, error: str = "") -> str:
    """The login form."""
    form = post_form(
        view,
        "/login",
        '<label for="email">E-mail address</label>'
        '<input id="email" name="email" type="email" autocomplete="username" required autofocus>'
        '<label for="password">Password</label>'
        '<input id="password" name="password" type="password" autocomplete="current-password" required>'
        '<p><button class="primary" type="submit">Log in</button></p>',
    )
    return layout(
        view, "Log in", f'<div class="login card"><h1>CCP Explorer</h1>{form}</div>', error, True
    )


def message_page(view: View, title: str, text: str) -> str:
    """A page with one short message (not found, not allowed ...)."""
    return layout(view, title, f"<h1>{esc(title)}</h1><p>{esc(text)}</p>")


# ---------------------------------------------------------------- home and project


def home_page(view: View, rows: list[Row]) -> str:
    """The tree Claude accounts -> projects with chat counts, tokens and last activity."""
    privileged = bool(view.user and view.user.reads_everything)
    own = view.user.account_id if view.user else None
    groups: dict[object, list[Row]] = {}
    for row in rows:
        groups.setdefault(row["account_id"], []).append(row)
    parts = ["<h1>Claude accounts and projects</h1>"]
    # Only projects that have chats with messages are listed; the empty ones (VS Code opens and
    # closes chats without a prompt) are counted in a note.
    hidden = sum(int(r["empty_chats"]) for r in rows)
    groups = {
        a: [r for r in members if int(r["chats"]) > 0]
        for a, members in groups.items()
        if any(int(r["chats"]) > 0 for r in members)
    }
    if not groups:
        parts.append('<p class="muted">Nothing has been captured yet.</p>')
    for account_id, members in groups.items():
        mine = ' <span class="badge">you</span>' if own is not None and account_id == own else ""
        parts.append(f"<h2>{esc(account_title(members[0], privileged))}{mine}</h2>")
        lines = []
        for r in members:
            if r["project_id"] is None:
                target = view.url(
                    "/project/0", account=account_id if account_id is not None else "none"
                )
            else:
                target = view.url(
                    f"/project/{int(r['project_id'])}",
                    account=account_id if account_id is not None else "none",
                )
            lines.append(
                [
                    f'<a href="{target}">{esc(project_title(r))}</a>',
                    num(r["chats"]),
                    num(r["empty_chats"]),
                    num(r["tokens"]),
                    when(r["last_activity_at"]),
                ]
            )
        parts.append(
            table(
                ["Project", "Chats", "Empty", "Tokens (in + out)", "Last activity"],
                lines,
                {1, 2, 3},
            )
        )
    if hidden:
        parts.append(
            f'<p class="muted">{hidden} chat(s) without any message are hidden. '
            "Tokens count what was sent and written; cache reads are on each chat's Usage tab.</p>"
        )
    return layout(view, "Home", "".join(parts))


def project_page(
    view: View,
    project: Row | None,
    account: str | None,
    chats: list[Row],
    show_empty: bool,
    open_account: int | None,
) -> str:
    """The chats of one project, with counts. Content links only where the visitor may read."""
    user = _visitor(view)
    privileged = user.reads_everything
    heading = project_title(project) if project else "Chats without a project"
    toggle = view.url(
        "/project/" + (str(int(project["project_id"])) if project else "0"),
        account=account,
        show_empty=None if show_empty else 1,
    )
    parts = [
        f"<h1>{esc(heading)}</h1>",
        f'<p><a href="{toggle}">{"Hide" if show_empty else "Show"} empty chats</a></p>',
    ]
    lines = []
    for c in chats:
        readable = user.may_open_chat(c["account_id"])
        title = (
            f'<a href="{view.url("/chat/" + quote(str(c["chat_id"])))}">{esc(chat_title(c))}</a>'
            if readable
            else '<span class="muted">(content hidden)</span>'
        )
        if c["is_empty"]:
            title += ' <span class="badge">empty</span>'
        who = "" if account else f"{esc(account_title(c, privileged))}<br>"
        lines.append(
            [
                when(c["started_at"] or c["last_activity_at"]),
                who + title,
                num(c["sessions"]),
                num(c["prompts"]),
                num(c["tokens"]),
                f'<span class="ok">{num(c["tests_passed"])}</span> / <span class="bad">{num(c["tests_failed"])}</span>',
                num(c["decisions"]),
                num(c["corrections"]),
            ]
        )
    headers = [
        "Date",
        "Chat",
        "Sessions",
        "Prompts",
        "Tokens (in + out)",
        "Tests ok / failed",
        "Decisions",
        "Corrections",
    ]
    parts.append(
        table(headers, lines, {2, 3, 4, 6, 7}) if lines else '<p class="muted">No chats.</p>'
    )
    if not privileged and open_account is None:
        parts.append(
            '<p class="muted">Your user is not linked to a Claude account: you see counts only.</p>'
        )
    return layout(view, heading, "".join(parts))


# ---------------------------------------------------------------- the chat page


def chat_header(view: View, chat: Row, tab: str) -> str:
    """The title, facts and tab bar of a chat page."""
    privileged = bool(view.user and view.user.reads_everything)
    facts = (
        f'{esc(account_title(chat, privileged))} / <a href="'
        + view.url(
            "/project/" + str(int(chat["project_id"]))
            if chat["project_id"] is not None
            else "/project/0"
        )
        + f'">{esc(project_title(chat))}</a> &middot; {when(chat["started_at"])} to {when(chat["last_activity_at"])}'
        f" &middot; {num(chat['sessions'])} sessions &middot; {num(chat['prompts'])} prompts"
        f" &middot; {num(chat['tokens'])} tokens (in + out)"
    )
    tabs = "".join(
        f'<a href="{view.url("/chat/" + quote(str(chat["chat_id"])), tab=key)}"'
        f"{' class=on' if key == tab else ''}>{esc(label)}</a>"
        for key, label in TABS
    )
    return f'<h1>{esc(chat_title(chat))}</h1><p class="muted">{facts}</p><nav class="tabs">{tabs}</nav>'


def chat_page(view: View, chat: Row, tab: str, body: str, flash: str = "") -> str:
    """A chat page: header and tabs around the tab's body."""
    return layout(view, chat_title(chat), chat_header(view, chat, tab) + body, flash)


def _visitor(view: View) -> UiUser:
    """The logged-in user of a page that only logged-in users reach."""
    if view.user is None:
        raise PermissionError("this page needs a logged-in user")
    return view.user


def _cut(text: str) -> str:
    """A message cut at the display limit, keeping its line breaks."""
    return text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"


def _stream_of(key: str) -> str:
    """The stream name in a raw object key (``sessions/<id>/main/000.jsonl`` gives ``main``)."""
    parts = key.split("/")
    return parts[2] if len(parts) > 3 else key


def _content_text(content: Any) -> tuple[str, list[str]]:
    """The text and the one-line summaries of tool calls in a message's content."""
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    calls: list[str] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            calls.append(summarise_call(str(block.get("name", "tool")), block.get("input")))
    return "\n\n".join(texts), calls


def summarise_call(name: str, tool_input: Any) -> str:
    """A tool call as one short line: the tool and its most telling argument."""
    arguments = tool_input if isinstance(tool_input, dict) else {}
    for key in ("command", "file_path", "path", "pattern", "url", "description", "query", "prompt"):
        if isinstance(arguments.get(key), str):
            return f"{name}: {short(arguments[key], 140)}"
    return name


def conversation_tab(view: View, chat_id: str, events: list[Row], total: int, page: int) -> str:
    """Prompts and replies in order; tool calls and results as one-line summaries."""
    parts: list[str] = []
    for e in events:
        kind, content = e["event_type"], e["content"]
        text, calls = _content_text(content)
        css = "abandoned" if e["is_abandoned"] else ""
        stamp = (
            f'<div class="meta">{when(e["occurred_at"])} &middot; {esc(e["actor"])}'
            + (" &middot; rewound" if e["is_abandoned"] else "")
            + "</div>"
        )
        if kind == "prompt":
            parts.append(f'<div class="msg prompt {css}">{stamp}{esc(_cut(text))}</div>')
        elif kind == "response" or (kind == "tool_call" and text):
            parts.append(f'<div class="msg response {css}">{stamp}{esc(_cut(text))}</div>')
        if kind == "tool_call":
            parts.extend(f'<div class="msg tool {css}">{esc(c)}</div>' for c in calls)
        elif kind == "tool_result":
            if (
                e["denial"] or e["is_error"]
            ):  # a result that worked adds nothing: only problems show
                label = "refused: " + str(e["denial"]) if e["denial"] else "failed"
                parts.append(f'<div class="msg tool {css}">tool result: {esc(label)}</div>')
        elif kind in (
            "slash_command",
            "command_output",
            "agent_message",
            "compaction_summary",
            "compact_boundary",
        ):
            line = text or str(content or "")
            parts.append(
                f'<div class="msg tool {css}">{esc(kind.replace("_", " "))}: {esc(short(line, 300))}</div>'
            )
    if not parts:
        parts.append('<p class="muted">No messages.</p>')
    pages = max((total + CONVERSATION_PAGE_SIZE - 1) // CONVERSATION_PAGE_SIZE, 1)
    if pages > 1:
        base = "/chat/" + quote(chat_id)
        prev = (
            f'<a href="{view.url(base, tab="conversation", page=page - 1)}">previous</a>'
            if page > 1
            else ""
        )
        nxt = (
            f'<a href="{view.url(base, tab="conversation", page=page + 1)}">next</a>'
            if page < pages
            else ""
        )
        parts.append(f'<p class="muted">Page {page} of {pages} {prev} {nxt}</p>')
    return "".join(parts)


def sessions_tab(rows: list[Row]) -> str:
    """The sessions of a chat: each time it was opened."""
    lines = [
        [
            num(r["seq"]),
            esc(r["start_source"]),
            when(r["started_at"]),
            when(r["ended_at"]),
            esc(r["end_reason"] or "-"),
            num(r["prompts"]),
            "recorded by the client" if r["recorded"] else "inferred",
        ]
        for r in rows
    ]
    head = ["#", "Started by", "Start", "End", "Ended by", "Prompts", "Source"]
    return table(head, lines, {0, 5}) if lines else '<p class="muted">No sessions recorded yet.</p>'


def _who_reviewed(d: Row) -> str:
    """Say who reviewed a decision: the AI (with its model), a person, or nobody yet."""
    status, kind = str(d["status"]), d["reviewed_by_kind"]
    if kind == "ai":
        return f"{status.replace('_', ' ')} by AI ({esc(d['reviewer'] or 'unknown model')})"
    if kind == "human":
        return f"{status.replace('_', ' ')} by {esc(d['reviewer'])}"
    return status.replace("_", " ") + " (not reviewed yet)"


def decisions_tab(view: View, chat_id: str, rows: list[Row], may_review: bool) -> str:
    """Decisions as cards, with the reviewer, the history and (for reviewers) the review form."""
    if not rows:
        return '<p class="muted">No decisions have been extracted for this chat yet.</p>'
    cards = []
    for d in rows:
        options = "".join(
            f"<li>{esc(o if isinstance(o, str) else json.dumps(o))}</li>"
            for o in as_json_list(d["options"])
        )
        evidence = ", ".join(
            f"<code>{esc(_stream_of(str(e.get('key', ''))))}:{esc(e.get('line'))}</code>"
            for e in as_json_list(d["evidence_lines"])
            if isinstance(e, dict)
        )
        history = "".join(
            f"<li>{esc(h.get('at'))}: {esc(h.get('status'))} by {esc(h.get('by_kind'))} "
            f"{esc(h.get('reviewer'))}{' - ' + esc(h.get('note')) if h.get('note') else ''}</li>"
            for h in as_json_list(d["review_history"])
            if isinstance(h, dict)
        )
        fields = [
            ("Goal", d["goal"]), ("Chosen", d["chosen"]), ("Reason", d["reason"]),
            ("Result", d["result"]), ("Tests", d["tests_summary"]),
            ("Decided by", d["decided_by"]),
        ]  # fmt: skip
        body = "".join(f"<p><strong>{k}:</strong> {esc(v)}</p>" for k, v in fields if v)
        if options:
            body += f"<p><strong>Options considered:</strong></p><ul>{options}</ul>"
        files = [f for f in as_json_list(d["files"]) if isinstance(f, dict)]
        if files:
            items = "".join(
                f"<li><code>{esc(f.get('path'))}</code> &ndash; {esc(f.get('change'))} "
                f'<span class="muted">({"by command" if f.get("via") == "shell" else "by edit"})'
                "</span></li>"
                for f in files
            )
            body += f"<p><strong>Files changed:</strong></p><ul>{items}</ul>"
        if d["commit_shas"]:
            body += (
                "<p><strong>Commits:</strong> "
                + ", ".join(f"<code>{esc(str(s)[:10])}</code>" for s in d["commit_shas"])
                + "</p>"
            )
        if evidence:
            body += f"<p><strong>Evidence (stream:line):</strong> {evidence}</p>"
        if d["review_note"]:
            body += f'<p class="muted">Review note: {esc(d["review_note"])}</p>'
        if history:
            body += f"<details><summary>Review history</summary><ul>{history}</ul></details>"
        if may_review:
            body += post_form(
                view,
                f"/chat/{quote(chat_id)}/decisions/{int(d['decision_id'])}/review",
                '<label>Note (optional)</label><textarea name="note" maxlength="500"></textarea>'
                '<p><button class="primary" name="status" value="approve">Approve</button> '
                '<button name="status" value="reject">Reject</button></p>',
            )
        cards.append(
            f'<div class="card"><h2>{esc(d["seq"])}. {esc(d["title"] or "Decision")} '
            f'{badge(d["status"])}</h2><p class="muted">{_who_reviewed(d)}</p>{body}</div>'
        )
    return "".join(cards)


def tests_tab(runs: list[Row], cases: list[Row]) -> str:
    """Test runs with their counts; the test cases of each run in a fold-out."""
    if not runs:
        return '<p class="muted">No test runs.</p>'
    by_run: dict[int, list[Row]] = {}
    for c in cases:
        by_run.setdefault(int(c["test_run_id"]), []).append(c)
    lines = []
    for r in runs:
        state = "unknown" if r["passed"] is None else ("passed" if r["passed"] else "failed")
        found = by_run.get(int(r["test_run_id"]), [])
        detail = ""
        if found:
            items = "".join(
                f"<li>{badge(c['outcome'])} <code>{esc(c['name'])}</code>"
                + (f' <span class="muted">{esc(c["message"])}</span>' if c["message"] else "")
                + "</li>"
                for c in found
            )
            detail = (
                f"<details><summary>{len(found)} test cases</summary><ul>{items}</ul></details>"
            )
        lines.append(
            [
                when(r["occurred_at"]),
                esc(r["framework"]),
                f"<code>{esc(short(r['command'], 90))}</code>" + detail,
                badge(state),
                num(r["tests_passed"]),
                num(r["tests_failed"]),
                num(r["tests_skipped"]),
                esc(r["output_source"] or "-"),
            ]
        )
    head = ["Time", "Runner", "Command", "Result", "Passed", "Failed", "Skipped", "Output"]
    return table(head, lines, {4, 5, 6})


def code_tab(changes: list[Row], commits: list[Row]) -> str:
    """File changes with their role, and the commits linked to the chat."""
    if not changes:
        return '<p class="muted">No file changes.</p>'
    totals: dict[str, int] = {}
    for c in changes:
        totals[str(c["file_role"] or "unclassified")] = (
            totals.get(str(c["file_role"] or "unclassified"), 0) + 1
        )
    summary = " ".join(f"{badge(role)} {count}" for role, count in sorted(totals.items()))
    lines = [
        [
            when(c["occurred_at"]),
            f"<code>{esc(c['path'])}</code>",
            badge(c["file_role"] or "unclassified"),
            esc(c["change_type"]),
            esc(
                c["actor_class"] + (" (sub-agent)" if str(c["stream"]).startswith("agent-") else "")
            ),
            f"+{num(c['lines_added'])} / -{num(c['lines_removed'])}",
            num(c["tests_added"]),
        ]
        for c in changes
    ]
    out = f"<p>{summary}</p>" + table(
        ["Time", "File", "Role", "Change", "By", "Lines", "Tests added"], lines, {6}
    )
    if commits:
        rows = [
            [f"<code>{esc(str(k['commit_sha'])[:10])}</code>", esc(k["method"]), esc(k["confidence"]),
             f'{num(k["matched_lines"])} / {num(k["total_lines"])}', when(k["committed_at"])]
            for k in commits
        ]  # fmt: skip
        out += "<h2>Linked commits</h2>" + table(
            ["Commit", "Method", "Confidence", "Lines matched", "Committed"], rows, {3}
        )
    return out


def corrections_tab(rows: list[Row]) -> str:
    """Things that had to be put right."""
    if not rows:
        return '<p class="muted">No corrections found.</p>'
    lines = [
        [esc(r["kind"].replace("_", " ")), esc(r["corrected_by"]), esc(r["detected_by"]),
         when(r["trigger_at"]), when(r["fix_at"]), esc(r["note"] or "")]
        for r in rows
    ]  # fmt: skip
    return table(["Kind", "Corrected by", "Found by", "Happened", "Fixed", "Note"], lines)


def agents_tab(agents: list[Row], workflows: list[Row]) -> str:
    """Sub-agents (with their reports) and Workflow runs."""
    out = ""
    if workflows:
        rows = [
            [esc(w["workflow_name"] or "-"), f"<code>{esc(str(w['workflow_run_id'])[:12])}</code>",
             esc(w["status"] or "-"), num(w["agent_count"]), num(w["event_count"]), when(w["started_at"]), when(w["ended_at"])]
            for w in workflows
        ]  # fmt: skip
        out += "<h2>Workflow runs</h2>" + table(
            ["Workflow", "Run", "Status", "Agents", "Events", "Start", "End"], rows, {3, 4}
        )
    if agents:
        rows = [
            [esc(a["agent_type"] or "-"),
             esc(a["description"] or "") + (f'<details><summary>Report</summary><pre>{esc(a["result"])}</pre></details>' if a["result"] else ""),
             esc(a["model"] or "-"), esc(a["effort"] or "-"), esc(a["workflow_name"] or "-"),
             num(a["event_count"]), when(a["started_at"])]
            for a in agents
        ]  # fmt: skip
        out += "<h2>Sub-agents</h2>" + table(
            ["Type", "Task", "Model", "Effort", "Workflow", "Events", "Start"], rows, {5}
        )
    return out or '<p class="muted">No sub-agents or workflows.</p>'


def thinking_tab(rows: list[Row]) -> str:
    """Thinking notes: context only, never proof of why the AI acted."""
    note = '<p class="muted">Summarised reasoning as Claude Code stored it. It is context, not proof of why an action was taken.</p>'
    parts = []
    for t in rows:
        where = "main" if t["stream"] == "main" else "sub-agent"
        text = (
            "(content withheld by the model)"
            if t["is_redacted"]
            else (t["text"] or "(no text stored)")
        )
        parts.append(
            f'<div class="msg {"abandoned" if t["is_abandoned"] else ""}"><div class="meta">{when(t["occurred_at"])} &middot; {where}</div>{esc(_cut(str(text)))}</div>'
        )
    return note + ("".join(parts) or '<p class="muted">No thinking notes.</p>')


def usage_tab(data: dict[str, list[Row]]) -> str:
    """Tokens by model, replies by effort, cost record, slash commands."""
    models = [
        [esc(m["model"]), num(m["input_tokens"]), num(m["output_tokens"]), num(m["cache_read_tokens"]),
         num(m["cache_creation_tokens"]), num(m["total_tokens"]), esc(m["cost_usd"] if m["cost_usd"] is not None else "-")]
        for m in data["models"]
    ]  # fmt: skip
    efforts = [
        [esc(e["effort"] or "not recorded"), esc(e["model"] or "-"), esc(e["requested_model"] or "-"),
         num(e["replies"]), num(e["output_tokens"]), num(e["thinking_ms"])]
        for e in data["effort"]
    ]  # fmt: skip
    out = "<h2>Tokens by model</h2>" + (
        table(
            ["Model", "Input", "Output", "Cache read", "Cache write", "Total", "Cost USD"],
            models,
            {1, 2, 3, 4, 5, 6},
        )
        if models
        else '<p class="muted">No usage recorded.</p>'
    )
    out += "<h2>Replies by effort</h2>" + (
        table(
            ["Effort", "Model", "Requested model", "Replies", "Output tokens", "Thinking ms"],
            efforts,
            {3, 4, 5},
        )
        if efforts
        else '<p class="muted">No replies.</p>'
    )
    if data["cost"]:
        c = data["cost"][0]
        out += (
            f'<p class="muted">Recorded cost: {esc(c["total_cost_usd"])} USD'
            f"{' (some models had no known price)' if c['has_unknown_model_cost'] else ''}; "
            f"lines added {num(c['lines_added'])}, removed {num(c['lines_removed'])}.</p>"
        )
    if data["commands"]:
        cmds = [
            [
                when(c["occurred_at"]),
                f"<code>{esc(c['name'])}</code>",
                esc(short(c["arguments"], 80)),
            ]
            for c in data["commands"]
        ]
        out += "<h2>Slash commands</h2>" + table(["Time", "Command", "Arguments"], cmds)
    return out


# ---------------------------------------------------------------- administration


def users_page(
    view: View, users: list[UiUser], accounts: list[Row], flash: str = "", error: bool = False
) -> str:
    """Create users; change role, account, state and password of each."""

    def account_select(selected: int | None) -> str:
        options = '<option value="">(none)</option>' + "".join(
            f'<option value="{int(a["account_id"])}"{" selected" if a["account_id"] == selected else ""}>'
            f"{esc(a['label'] or a['email'] or a['account_uuid'])}</option>"
            for a in accounts
        )
        return f'<select name="account_id">{options}</select>'

    def role_select(selected: str) -> str:
        return (
            '<select name="role">'
            + "".join(f"<option{' selected' if r == selected else ''}>{r}</option>" for r in ROLES)
            + "</select>"
        )

    rows = []
    for u in users:
        prefix = f"/admin/users/{u.user_id}"
        rows.append(
            [
                esc(u.email) + ("" if not u.disabled else " " + badge("disabled")),
                post_form(
                    view,
                    prefix + "/role",
                    role_select(u.role) + ' <button type="submit">Set</button>',
                    "inline",
                ),
                post_form(
                    view,
                    prefix + "/account",
                    account_select(u.account_id) + ' <button type="submit">Link</button>',
                    "inline",
                ),
                post_form(
                    view,
                    prefix + "/disable",
                    f'<input type="hidden" name="disabled" value="{"0" if u.disabled else "1"}">'
                    f'<button type="submit">{"Enable" if u.disabled else "Disable"}</button>',
                    "inline",
                ),
                post_form(
                    view,
                    prefix + "/password",
                    '<input name="password" type="password" autocomplete="new-password" '
                    'placeholder="new password" minlength="12" required> <button type="submit">Reset</button>',
                    "inline",
                ),
            ]
        )
    create = post_form(
        view,
        "/admin/users",
        '<div class="cols"><div><label>E-mail address</label><input name="email" type="email" required></div>'
        '<div><label>Name</label><input name="full_name"></div>'
        f"<div><label>Role</label>{role_select('developer')}</div>"
        f"<div><label>Claude account</label>{account_select(None)}</div>"
        '<div><label>Password (12+ characters)</label><input name="password" type="password" autocomplete="new-password" minlength="12" required></div></div>'
        '<p><button class="primary" type="submit">Create user</button></p>',
    )
    body = (
        "<h1>Users</h1>"
        + table(["User", "Role", "Claude account", "State", "Password"], rows)
        + f'<h2>New user</h2><div class="card">{create}</div>'
    )
    return layout(view, "Users", body, flash, error)


def labels_page(
    view: View, accounts: list[Row], projects: list[Row], flash: str = "", error: bool = False
) -> str:
    """Set or remove the labels of accounts and projects."""

    def row(kind: str, key: str, name: str, label: object) -> list[str]:
        form = post_form(
            view,
            "/admin/labels",
            f'<input type="hidden" name="kind" value="{kind}"><input type="hidden" name="key" value="{esc(key)}">'
            f'<input name="label" value="{esc(label)}" maxlength="40" placeholder="no label"> '
            '<button type="submit">Save</button>',
            "inline",
        )
        return [esc(name), form]

    account_rows = [
        row(
            "account",
            str(a["email"] or a["account_uuid"]),
            str(a["display_name"] or a["full_name"] or a["email"] or a["account_uuid"]),
            a["label"],
        )
        for a in accounts
        if a["email"] or a["account_uuid"]
    ]
    project_rows = [
        row("project", str(p["project_key"]), str(p["name"]), p["label"]) for p in projects
    ]
    body = (
        "<h1>Labels</h1><p class=muted>A label is the name dashboards show instead of an e-mail address or folder. "
        "Leave it empty and save to remove it. 1 to 40 letters, digits, spaces, dots, dashes or underscores.</p>"
        "<h2>Claude accounts</h2>"
        + table(["Account", "Label"], account_rows)
        + "<h2>Projects</h2>"
        + table(["Project", "Label"], project_rows)
    )
    return layout(view, "Labels", body, flash, error)


def access_log_page(view: View, rows: list[Row], next_before: int | None) -> str:
    """Who opened what, newest first."""
    lines = [
        [
            when(r["at"]),
            esc(r["email"] or "(deleted user)"),
            esc(r["role"] or "-"),
            esc(r["action"]),
            f"<code>{esc(r['target'])}</code>",
        ]
        for r in rows
    ]
    more = (
        f'<p><a href="{view.url("/access-log", before=next_before)}">Older entries</a></p>'
        if next_before
        else ""
    )
    return layout(
        view,
        "Access log",
        "<h1>Access log</h1>" + table(["Time", "User", "Role", "Action", "Target"], lines) + more,
    )
