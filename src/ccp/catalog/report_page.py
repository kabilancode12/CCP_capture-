"""The report as a styled web page (``report.html``), for showing a session to a client.

The page holds the exact text of ``report.md`` and turns it into formatted HTML in the browser
(the ``marked`` library, pinned to one version). It adds colour and layout only: a header, a
contents bar, cards for sections, striped tables, and coloured labels for who made a change
(person, Claude edit, shell command, sub-agent) and for review states. Nothing is added to or taken
from the report. Without a network connection the page still shows the report as plain text.
"""

from __future__ import annotations

import html
import json

MARKED = "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"

_STYLE = """
:root{--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--bg:#f6f7fb;--card:#fff;--brand:#4f46e5;--brand2:#0ea5e9;
--human:#16a34a;--edit:#4f46e5;--cmd:#d97706;--sub:#db2777;--other:#64748b;--ok:#16a34a;--warn:#d97706;--bad:#dc2626}
@media (prefers-color-scheme:dark){:root{--ink:#e5e7eb;--muted:#9ca3af;--line:#374151;--bg:#0f172a;--card:#111827}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
header{background:linear-gradient(120deg,var(--brand),var(--brand2));color:#fff;padding:28px 32px}
header h1{margin:0 0 6px;font-size:26px}header p{margin:0;opacity:.9}
nav{position:sticky;top:0;z-index:2;background:var(--card);border-bottom:1px solid var(--line);
padding:8px 24px;display:flex;gap:6px;flex-wrap:wrap}
nav a{color:var(--brand);text-decoration:none;font-size:13px;padding:4px 10px;border-radius:999px;background:rgba(79,70,229,.08)}
main{max-width:1180px;margin:24px auto;padding:0 16px}
section.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:8px 24px 18px;
margin:0 0 20px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
h2{color:var(--brand);border-bottom:2px solid var(--line);padding-bottom:6px}
h3{margin-top:26px;padding:6px 10px;border-left:4px solid var(--brand2);background:rgba(14,165,233,.07);border-radius:4px}
h4{color:var(--muted);text-transform:uppercase;font-size:12px;letter-spacing:.06em}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13.5px;display:block;overflow-x:auto}
th{background:rgba(79,70,229,.1);text-align:left}th,td{border:1px solid var(--line);padding:6px 9px;vertical-align:top}
tr:nth-child(even) td{background:rgba(127,127,127,.05)}
blockquote{margin:8px 0;padding:8px 14px;border-left:4px solid var(--brand);background:rgba(79,70,229,.06);border-radius:4px}
pre{background:#0b1020;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12.5px}
code{font-family:ui-monospace,Menlo,Consolas,monospace}p code,td code{background:rgba(127,127,127,.12);padding:1px 4px;border-radius:4px}
details{border:1px solid var(--line);border-radius:8px;padding:6px 12px;margin:8px 0;background:rgba(127,127,127,.03)}
summary{cursor:pointer;font-weight:600;color:var(--ink)}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;color:#fff;font-size:12px;font-weight:600}
.t-human{background:var(--human)}.t-ai_edit{background:var(--edit)}.t-ai_command{background:var(--cmd)}
.t-subagent{background:var(--sub)}.t-ai_other{background:var(--other)}
.t-approved,.t-passed{background:var(--ok)}.t-needs_review{background:var(--warn)}.t-rejected,.t-failed{background:var(--bad)}
footer{color:var(--muted);text-align:center;font-size:12px;padding:24px}
"""

_SCRIPT = """
const source = JSON.parse(document.getElementById('report-source').textContent);
const target = document.getElementById('report');
const TAGS = new Set(['human','ai_edit','ai_command','subagent','ai_other','approved','needs_review','rejected']);
function render() {
  if (!window.marked) { target.innerHTML = '<pre></pre>'; target.firstChild.textContent = source; return; }
  const holder = document.createElement('div');
  holder.innerHTML = marked.parse(source, {gfm: true});
  const title = holder.querySelector('h1'); if (title) title.remove();
  const intro = holder.querySelector('blockquote'); if (intro) intro.remove();
  // one card per "## " section, and a link to it in the bar at the top
  const nav = document.getElementById('sections'); let card = null; let n = 0;
  for (const node of Array.from(holder.childNodes)) {
    if (node.nodeName === 'H2') {
      if (node.textContent.trim() === 'Contents') { card = null; continue; }
      card = document.createElement('section'); card.className = 'card'; card.id = 's' + (++n);
      target.appendChild(card);
      const link = document.createElement('a'); link.href = '#' + card.id; link.textContent = node.textContent;
      nav.appendChild(link);
    }
    if (card) card.appendChild(node);
  }
  // coloured labels for who made a change and for review states
  for (const cell of target.querySelectorAll('td')) {
    const word = cell.textContent.trim();
    if (TAGS.has(word)) cell.innerHTML = '<span class="tag t-' + word + '">' + word.replace('_', ' ') + '</span>';
  }
}
render();
"""


def report_page(title: str | None, markdown: str) -> str:
    """A complete HTML page for the report (``markdown`` is ``report.md`` exactly)."""
    name = html.escape(title or "Claude Code session")
    first = markdown.split("\n> ", 1)[1].split("\n", 1)[0] if "\n> " in markdown else ""
    # JSON keeps every character; "</" is escaped so the text cannot end the script element.
    source = json.dumps(markdown, ensure_ascii=False).replace("</", "<\\/")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{name}</title><style>{_STYLE}</style>"
        f'<script src="{MARKED}"></script></head><body>'
        f"<header><h1>{name}</h1><p>{html.escape(first)}</p></header>"
        '<nav id="sections"></nav><main id="report"></main>'
        "<footer>Generated by the Claude Capture Platform from the recorded session. "
        "Same content as report.md.</footer>"
        f'<script type="application/json" id="report-source">{source}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )
