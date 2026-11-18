"""Readable reports of one session from `capture.json`: report.md and report.html."""

from __future__ import annotations

import html
from pathlib import Path

from .transcript import dumps_input

CLIP = 3000  # characters of a tool result shown in the reports (capture.json/raw keep more)


def _clip(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= CLIP else text[:CLIP] + f"\n… ({len(text) - CLIP} more characters)"


def _tokens(t: dict) -> str:
    return (f"in {t['input_tokens']:,} · out {t['output_tokens']:,} · "
            f"cache read {t['cache_read_input_tokens']:,} · "
            f"cache write {t['cache_creation_input_tokens']:,}")


def _fence(text: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text}\n{ticks}"


def to_markdown(cap: dict) -> str:
    """Session as Markdown: header, then each turn with prompt, steps and file changes."""
    s = cap["session"]
    out = [f"# {s.get('title') or 'Claude Code session'}", "",
           f"- Session: `{s.get('session_id')}`", f"- Folder: `{s.get('cwd')}`",
           f"- Git branch: `{s.get('git_branch')}` · Claude Code {s.get('claude_version')}",
           f"- Turns: {cap['turn_count']} · compactions: {s.get('compactions', 0)}", ""]
    for t in cap["turns"]:
        out += [f"## Turn {t['turn']} — {t['started_at']}", "",
                f"*Models:* {', '.join(t['models']) or '-'} · *Tokens:* {_tokens(t['tokens'])}",
                "", "### Prompt", "", _fence(t["prompt"]), ""]
        for step in t["steps"]:
            if step["kind"] == "thinking":
                out += ["#### Thinking", "", _fence(step["text"]), ""]
            elif step["kind"] == "text":
                out += ["#### Claude", "", step["text"], ""]
            else:
                snap = f" → snapshot `{step['snapshot']}`" if step.get("snapshot") else ""
                err = " **(error)**" if step.get("is_error") else ""
                out += [f"#### Tool: {step['name']}{err}{snap}", "",
                        _fence(dumps_input(step["input"])), "", "Result:", "",
                        _fence(_clip(step.get("result"))), ""]
        if t["changes"]:
            out += ["### File changes in this turn", ""]
            for c in t["changes"]:
                out.append(f"- `{c['commit'][:10]}` by **{c['by']}** ({c['label']}), "
                           f"{c['file_count']} file(s): " + ", ".join(f"`{f}`" for f in c["files"]))
            out.append("")
    return "\n".join(out)


def _pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def to_html(cap: dict) -> str:
    """Same content as the Markdown, as one self-contained page (light and dark)."""
    s = cap["session"]
    e = html.escape
    body = [f"<h1>{e(s.get('title') or 'Claude Code session')}</h1>",
            f"<p class=meta>Session {e(str(s.get('session_id')))} · {e(str(s.get('cwd')))} · "
            f"branch {e(str(s.get('git_branch')))} · {cap['turn_count']} turns</p>"]
    for t in cap["turns"]:
        body.append(f"<section><h2>Turn {t['turn']} <small>{e(t['started_at'] or '')}</small></h2>"
                    f"<p class=meta>{e(', '.join(t['models']) or '-')} · {_tokens(t['tokens'])}</p>"
                    f"<div class=prompt>{_pre(t['prompt'])}</div>")
        for step in t["steps"]:
            if step["kind"] == "thinking":
                body.append(f"<details><summary>Thinking</summary>{_pre(step['text'])}</details>")
            elif step["kind"] == "text":
                body.append(f"<div class=reply>{_pre(step['text'])}</div>")
            else:
                snap = f" · snapshot {e(step['snapshot'])}" if step.get("snapshot") else ""
                err = " <b class=err>error</b>" if step.get("is_error") else ""
                body.append(f"<details><summary>Tool <b>{e(step['name'] or '')}</b>{err}{snap}"
                            f"</summary>{_pre(dumps_input(step['input']))}"
                            f"<p>Result</p>{_pre(_clip(step.get('result')))}</details>")
        if t["changes"]:
            items = "".join(f"<li><code>{e(c['commit'][:10])}</code> by <b>{e(str(c['by']))}</b> "
                            f"({e(str(c['label']))}): {e(', '.join(c['files']))}</li>"
                            for c in t["changes"])
            body.append(f"<h3>File changes</h3><ul>{items}</ul>")
        body.append("</section>")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(s.get('title') or 'CCP session')}</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mute:#666;--box:#f4f4f6;--line:#ddd;--acc:#2f5bd3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#e8e8ea;--mute:#9a9aa0;
--box:#212125;--line:#333;--acc:#8ab0ff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;
max-width:960px;margin:0 auto;padding:16px}}
section{{border-top:1px solid var(--line);margin-top:24px}}
pre{{background:var(--box);padding:10px;border-radius:6px;white-space:pre-wrap;
word-break:break-word;font-size:13px}}
.prompt pre{{border-left:3px solid var(--acc)}} .meta,small{{color:var(--mute)}}
summary{{cursor:pointer;margin:6px 0}} .err{{color:#d33}}
</style></head><body>{''.join(body)}</body></html>"""


def write(sdir: Path, cap: dict) -> None:
    """Write report.md and report.html into the session folder."""
    (sdir / "report.md").write_text(to_markdown(cap), encoding="utf-8")
    (sdir / "report.html").write_text(to_html(cap), encoding="utf-8")
