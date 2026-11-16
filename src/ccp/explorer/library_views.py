# ruff: noqa: E501  (HTML and SQL text is clearer on one line)
"""Explorer pages for the approach library and for what the capture chain recorded.

Approaches, the decision timeline, the review queue, who is working where, insights and alerts.
Every handler takes the request context and returns a page; the data comes from
:mod:`ccp.approaches` and from the ``shadow_commit`` table. Approaches show decision summaries (goal,
choice, reason), which every signed-in user may read so that approaches can be reused across
developers; the thinking notes and chat links are shown to leads, auditors and admins only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from starlette.responses import Response

from ccp.approaches import library
from ccp.explorer.pages import esc, layout, message_page, num, short, table, when

if TYPE_CHECKING:  # the application imports this module, so only the type checker may import it
    from ccp.explorer.app import Context

STATE = {"active": "Active", "superseded": "Replaced", "removed": "Removed"}
_WORD = re.compile(r"[a-z0-9]{3,}")


def _rows(connection: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    cursor = connection.cursor()
    cursor.execute(sql, params)
    return [tuple(row) for row in cursor.fetchall()]


def _login(ctx: Context) -> Response | None:
    return ctx.redirect("/login") if ctx.user is None else None


def _link(ctx: Context, path: str, text: object, **query: object) -> str:
    return f'<a href="{ctx.view.url(path, **query)}">{esc(text)}</a>'


# ---------------------------------------------------------------- approaches


def approaches(ctx: Context) -> Response:
    """Search the approach library (every word must appear)."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    text, removed = ctx.query("q") or "", ctx.query("removed") == "1"
    found = library.search(ctx.connection, text, only_removed=removed, limit=100)
    form = (
        f'<form method="get" action="{ctx.view.url("/approaches")}">'
        f'<input name="q" value="{esc(text)}" placeholder="for example: parser 10kb" size="40"> '
        f'<label><input type="checkbox" name="removed" value="1"{" checked" if removed else ""}>'
        ' only replaced or removed</label> <button type="submit">Search</button></form>'
    )
    rows = [
        [
            _link(ctx, f"/approach/{a.decision_id}", f"#{a.decision_id}"),
            esc(STATE.get(a.lifecycle, a.lifecycle)),
            esc(a.title),
            esc(a.project),
            esc(a.decided_by),
            esc(short(a.reason, 120)),
            esc(a.removed_by or ""),
            num(a.reuses),
        ]
        for a in found
    ]
    body = (
        '<h1>Approach library</h1><p class="muted">A decision with its code, including code that '
        "was never committed or was later removed. Open one to see why it was chosen and how to "
        f"restore it.</p>{form}"
        + table(
            ["#", "State", "Approach", "Project", "Decided by", "Why", "Replaced by", "Reused"],
            rows,
            {7},
        )
    )
    return ctx.page(layout(ctx.view, "Approaches", body))


def approach(ctx: Context) -> Response:
    """One approach in full, with the command to restore it."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    item = library.get(ctx.connection, int(ctx.request.path_params["decision_id"]))
    if item is None:
        return ctx.page(message_page(ctx.view, "Not found", "There is no such approach."), 404)
    privileged = bool(ctx.user and ctx.user.reads_everything)
    facts = [
        ("State", STATE.get(item.lifecycle, item.lifecycle)),
        ("Project", item.project), ("Account", item.account), ("Decided by", item.decided_by),
        ("Written by model", item.model), ("Review", item.status), ("Goal", item.goal),
        ("Chosen", item.chosen), ("Why", item.reason), ("Result", item.result),
        ("Tests", item.tests_summary),
        ("Files", ", ".join(p for p, _ in item.file_paths())),
        ("Code", f"capture commit {item.code_sha}" if item.code_sha else "not on the capture chain"),
    ]  # fmt: skip
    rows = [[esc(k), esc(v)] for k, v in facts]
    body = f"<h1>#{item.decision_id} {esc(item.title)}</h1>" + table(["", ""], rows)
    if item.options:
        body += (
            "<h2>Options considered</h2><ul>"
            + "".join(f"<li>{esc(o)}</li>" for o in item.options)
            + "</ul>"
        )
    if item.lifecycle != "active":
        by = (
            f" by {_link(ctx, f'/approach/{item.removed_by_id}', f'#{item.removed_by_id} {item.removed_by}')}"
            if item.removed_by_id
            else ""
        )
        body += f"<h2>What became of it</h2><p>Removed {when(item.removed_at)} in commit <code>{esc((item.removed_in_sha or '')[:10])}</code>{by}.</p>"
    if item.supersedes_id:
        body += f"<p>It replaced {_link(ctx, f'/approach/{item.supersedes_id}', f'#{item.supersedes_id} {item.supersedes}')}.</p>"
    if privileged:
        notes = [n["text"] for n in item.thinking or [] if isinstance(n, dict) and n.get("text")]
        if notes:
            body += "<h2>Claude's thinking at the time</h2>" + "".join(
                f"<blockquote>{esc(short(n, 700))}</blockquote>" for n in notes
            )
        body += f"<p>{_link(ctx, f'/chat/{item.session_id}', 'Open the chat', tab='decisions')}</p>"
    if item.reuse_log:
        body += "<h2>Reused</h2>" + table(
            ["When", "By", "For", "Where"],
            [
                [when(u["reused_at"]), esc(u["reused_by"]), esc(u["purpose"]), esc(u["target"])]
                for u in item.reuse_log
            ],
        )
    body += (
        "<h2>Restore it</h2><p>Run in a clone of the capture project:</p><pre>"
        f"ccp-approaches restore {item.decision_id} --repo &lt;capture clone&gt; --into &lt;folder&gt; "
        "--by &lt;you&gt; --purpose &quot;what you need it for&quot;</pre>"
    )
    return ctx.page(layout(ctx.view, f"Approach {item.decision_id}", body))


def compare(ctx: Context) -> Response:
    """Two approaches side by side."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    ids = [ctx.query("a") or "", ctx.query("b") or ""]
    items = [library.get(ctx.connection, int(i)) if i.isdigit() else None for i in ids]
    if not all(items):
        return ctx.page(
            message_page(ctx.view, "Compare", "Give two approach numbers: ?a=1&b=2."), 404
        )
    first, second = items
    assert first is not None and second is not None  # noqa: S101  (checked above)
    fields: list[tuple[str, Callable[[library.Approach], object]]] = [
        ("State", lambda a: STATE.get(a.lifecycle, a.lifecycle)), ("Goal", lambda a: a.goal),
        ("Chosen", lambda a: a.chosen), ("Why", lambda a: a.reason), ("Result", lambda a: a.result),
        ("Tests", lambda a: a.tests_summary), ("Decided by", lambda a: a.decided_by),
        ("Model", lambda a: a.model), ("Files", lambda a: ", ".join(p for p, _ in a.file_paths())),
        ("Replaced by", lambda a: a.removed_by),
    ]  # fmt: skip
    rows = [[esc(name), esc(get(first)), esc(get(second))] for name, get in fields]
    heads = ["", f"#{first.decision_id} {first.title}", f"#{second.decision_id} {second.title}"]
    return ctx.page(layout(ctx.view, "Compare", "<h1>Compare approaches</h1>" + table(heads, rows)))


# ---------------------------------------------------------------- timeline, review, activity


def timeline(ctx: Context) -> Response:
    """The decisions of a project in time order, with what replaced what."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    project = ctx.query("project")
    rows = _rows(
        ctx.connection,
        "SELECT d.decision_id, d.created_at, d.title, d.lifecycle, d.decided_by, d.removed_by_decision_id, "
        "d.supersedes_decision_id, COALESCE(c.project_label, c.project_name) FROM decision d "
        "LEFT JOIN chat c ON c.chat_id = d.session_id WHERE d.status <> 'rejected' "
        "AND (%s::text IS NULL OR c.project_id::text = %s::text) ORDER BY d.created_at, d.decision_id LIMIT 500",
        (project, project),
    )
    cells = [
        [
            when(r[1]), _link(ctx, f"/approach/{r[0]}", f"#{r[0]} {r[2]}"), esc(STATE.get(r[3], r[3])),
            esc(r[4]), (f"replaced by #{r[5]}" if r[5] else "") + (f" replaces #{r[6]}" if r[6] else ""), esc(r[7]),
        ]
        for r in rows
    ]  # fmt: skip
    body = "<h1>Decision timeline</h1>" + table(
        ["When", "Decision", "State", "Decided by", "Lineage", "Project"], cells
    )
    return ctx.page(layout(ctx.view, "Timeline", body))


def review_queue(ctx: Context) -> Response:
    """Decisions that need a person to look at them."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    rows = _rows(
        ctx.connection,
        "SELECT d.decision_id, d.session_id, d.title, d.decided_by, d.review_note, d.created_at, "
        "COALESCE(c.project_label, c.project_name) FROM decision d LEFT JOIN chat c ON c.chat_id = d.session_id "
        "WHERE d.status = 'needs_review' ORDER BY d.created_at DESC LIMIT 200",
    )
    privileged = bool(ctx.user and ctx.user.may_review)
    cells = [
        [
            when(r[5]), esc(r[6]), esc(r[2]), esc(r[3]), esc(short(r[4], 160)),
            _link(ctx, f"/chat/{r[1]}", "review", tab="decisions") if privileged else "",
        ]
        for r in rows
    ]  # fmt: skip
    body = (
        '<h1>Review queue</h1><p class="muted">The AI reviewer was not sure about these.</p>'
        + table(["When", "Project", "Decision", "Decided by", "Reviewer's note", ""], cells)
    )
    return ctx.page(layout(ctx.view, "Review queue", body))


def activity(ctx: Context) -> Response:
    """Who is working where, from the capture chain: the latest change of each developer and branch."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    rows = _rows(
        ctx.connection,
        "SELECT chain, count(*), max(captured_at), "
        "count(*) FILTER (WHERE actor = 'human'), count(*) FILTER (WHERE actor <> 'human'), "
        "count(*) FILTER (WHERE captured_at > now() - interval '24 hours'), sum(files_total) "
        "FROM shadow_commit GROUP BY chain ORDER BY max(captured_at) DESC",
    )
    cells = [
        [
            esc(r[0] or "(unknown)"),
            when(r[2]),
            num(r[1]),
            num(r[3]),
            num(r[4]),
            num(r[5]),
            num(int(r[6] or 0)),
        ]
        for r in rows
    ]
    body = (
        '<h1>Capture activity</h1><p class="muted">Each row is one developer, machine and branch. '
        "Every change in their working folder is recorded here, committed or not. A row that is "
        "old with changes only on one machine is work that exists nowhere else.</p>"
        + table(
            [
                "Capture branch",
                "Last change",
                "Changes",
                "By a person",
                "By Claude",
                "Last 24 h",
                "Files touched",
            ],
            cells,
            {2, 3, 4, 5, 6},
        )
    )
    return ctx.page(layout(ctx.view, "Capture activity", body))


def insights(ctx: Context) -> Response:
    """Counts: who changes the code, with which model, and how approaches fare."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    c = ctx.connection
    by_actor = _rows(c, "SELECT actor, count(*) FROM shadow_commit GROUP BY actor ORDER BY 2 DESC")
    by_model = _rows(
        c,
        "SELECT COALESCE(model, '(none)'), count(*) FROM shadow_commit WHERE actor <> 'human' GROUP BY 1 ORDER BY 2 DESC",
    )
    states = _rows(
        c,
        "SELECT lifecycle, count(*) FROM decision WHERE status <> 'rejected' GROUP BY 1 ORDER BY 2 DESC",
    )
    deciders = _rows(
        c,
        "SELECT COALESCE(decided_by, '?'), count(*) FROM decision WHERE status <> 'rejected' GROUP BY 1 ORDER BY 2 DESC",
    )
    reused = _rows(
        c, "SELECT approach_title, count(*) FROM approach_reuse GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
    )

    def block(title: str, heads: list[str], data: list[tuple[Any, ...]]) -> str:
        return f"<h2>{esc(title)}</h2>" + table(heads, [[esc(a), num(b)] for a, b in data], {1})

    body = (
        "<h1>Insights</h1>"
        + block("Changes by who made them", ["Actor", "Changes"], by_actor)
        + block("Changes by Claude, by model", ["Model", "Changes"], by_model)
        + block("Decisions by who decided", ["Decided by", "Decisions"], deciders)
        + block("Approaches by state", ["State", "Approaches"], states)
        + block("Most reused approaches", ["Approach", "Times reused"], reused)
        + '<p class="muted">Usage, cost and test charts are in Metabase (see docs/dashboards.md).</p>'
    )
    return ctx.page(layout(ctx.view, "Insights", body))


def alerts(ctx: Context) -> Response:
    """Things worth a look: removed approaches that look like newer work, and open reviews."""
    if (redirect := _login(ctx)) is not None:
        return redirect
    rows = _rows(
        ctx.connection,
        "SELECT decision_id, title, lifecycle, created_at, session_id FROM decision WHERE status <> 'rejected' ORDER BY created_at",
    )
    words = {r[0]: set(_WORD.findall((r[1] or "").lower())) for r in rows}
    found: list[list[str]] = []
    for old in rows:
        if old[2] == "active" or len(words[old[0]]) < 2:
            continue
        for new in rows:
            if new[3] <= old[3] or new[0] == old[0] or new[4] == old[4]:
                continue
            shared = words[old[0]] & words[new[0]]
            if len(shared) >= 2 and len(shared) / len(words[old[0]] | words[new[0]]) >= 0.5:
                found.append(
                    [
                        _link(ctx, f"/approach/{new[0]}", f"#{new[0]} {new[1]}"),
                        "looks like the removed approach",
                        _link(ctx, f"/approach/{old[0]}", f"#{old[0]} {old[1]}"),
                    ]
                )
    waiting = _rows(ctx.connection, "SELECT count(*) FROM decision WHERE status = 'needs_review'")[
        0
    ][0]
    body = (
        "<h1>Alerts</h1><h2>Newer work that resembles a removed approach</h2>"
        + table(["New work", "", "Earlier, removed approach"], found)
        + f"<h2>Decisions waiting for a person</h2><p>{_link(ctx, '/review', f'{num(waiting)} in the review queue')}</p>"
    )
    return ctx.page(layout(ctx.view, "Alerts", body))


# ---------------------------------------------------------------- the capture chain of one chat


def changes_tab(ctx: Context, chat_id: str) -> str:
    """HTML of the chat's *Changes by cause* tab: the capture commits and who made each."""
    rows = _rows(
        ctx.connection,
        "SELECT captured_at, actor, tool, target, model, files_total, sha, files FROM shadow_commit "
        "WHERE session_id = %s ORDER BY captured_at, seq",
        (chat_id,),
    )
    cells = [
        [when(r[0]), esc(r[1]), esc(r[2] or ""), esc(short(r[3], 80)), esc(r[4] or ""), num(r[5]),
         esc(", ".join(f"{f['state']} {f['path']}" for f in (r[7] or [])[:6])), f"<code>{esc(r[6][:10])}</code>"]
        for r in rows
    ]  # fmt: skip
    return (
        '<p class="muted">Every change made to the working folder during this chat, one row each, with who '
        "made it: a person, a Claude edit tool, a shell command or a sub-agent. The code of each row is "
        "in the capture project (<code>git show &lt;commit&gt;</code>).</p>"
        + table(["When", "Who", "Tool", "What", "Model", "Files", "Changed", "Commit"], cells, {5})
    )
