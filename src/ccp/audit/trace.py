"""Exact line tracing on the capture chain: from a line of code to the chat that wrote it.

Every change a developer's working tree went through is one commit of the capture chain, and each
commit carries who made it and why (see ``docs/snapshots.md``). So ``git blame`` on the chain names,
for any line, the commit that last changed it, whether the change was made by Claude (an edit tool,
a shell command, a sub-agent) or by a person. The index then adds the prompt, Claude's thinking just
before the call, and the decisions that touched the file. Nothing is guessed: if the blamed commit
is not a capture commit, the line is reported as older than capture.

This module reads Git and the index; it changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ccp.audit.gitlog import GitError, _git
from ccp.audit.repository import AuditRepository
from ccp.catalog.conversation import text_of

PROMPT_MAX = 600
THINKING_BLOCKS = 3

_CHAIN_REFS = ("refs/heads/ccp", "refs/claude-capture/chain")


@dataclass(frozen=True)
class ShadowInfo:
    """What the index knows about a capture commit."""

    session_id: str
    actor: str
    developer: str | None
    model: str | None
    tool: str | None
    target: str | None
    tool_use_id: str | None
    prompt_uuid: str | None
    prompt_excerpt: str | None
    agent: str | None
    captured_at: datetime
    chain: str | None
    seq: int | None


@dataclass(frozen=True)
class Reasoning:
    """The conversation behind a capture commit."""

    prompt: str | None = None
    thinking: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LineOrigin:
    """Where one line of code came from."""

    commit: str
    author: str
    summary: str
    shadow: ShadowInfo | None
    reasoning: Reasoning = field(default_factory=Reasoning)


def newest_chain_ref(repository: Path) -> str:
    """The most recently updated capture chain in a repository (a clone or the capture project)."""
    out = _git(
        repository, "for-each-ref", "--sort=-committerdate", "--count=1",
        "--format=%(refname)", *_CHAIN_REFS,
    )  # fmt: skip
    ref = out.strip()
    if not ref:
        raise GitError("this repository has no capture chain (refs/heads/ccp/...)")
    return ref


def blame_line(repository: Path, ref: str, path: str, line: int) -> tuple[str, str, str]:
    """``(commit, author, summary)`` of the commit that last changed a line, by ``git blame``.

    Raises:
        GitError: If Git fails (no such file or line at that ref).
    """
    out = _git(repository, "blame", "--porcelain", "-L", f"{line},{line}", ref, "--", path)
    lines = out.splitlines()
    if not lines:
        raise GitError("git blame gave no answer")
    sha = lines[0].split()[0]
    info = {
        k: v for k, _, v in (item.partition(" ") for item in lines[1:] if not item.startswith("\t"))
    }
    return sha, info.get("author", "?"), info.get("summary", "")


def trace_line(
    repository: Path, ref: str, path: str, line: int, index: AuditRepository
) -> LineOrigin:
    """Trace one line to its capture commit and the chat behind it."""
    sha, author, summary = blame_line(repository, ref, path, line)
    shadow = index.shadow_commit(sha)
    if shadow is None:
        return LineOrigin(sha, author, summary, None)
    return LineOrigin(sha, author, summary, shadow, index.reasoning_of(shadow, path))


def describe(origin: LineOrigin, path: str, line: int) -> list[str]:
    """The answer as plain lines for the terminal."""
    shadow = origin.shadow
    if shadow is None:
        return [
            f"Line {line} of {path} is older than capture or was committed outside it:",
            f"  commit {origin.commit[:10]} by {origin.author}: {origin.summary}",
        ]
    who = {
        "human": "a person (outside Claude)",
        "ai_edit": f"Claude ({shadow.model or '?'}), edit tool {shadow.tool or ''}",
        "ai_command": f"Claude ({shadow.model or '?'}), shell command",
        "subagent": f"a sub-agent ({shadow.agent}; {shadow.model or '?'})",
    }.get(shadow.actor, f"Claude ({shadow.model or '?'})")
    when = shadow.captured_at.strftime("%Y-%m-%d %H:%M:%S")
    out = [
        f"Line {line} of {path} was last changed by {who}",
        f"  capture commit {origin.commit[:10]} at {when}, chat {shadow.session_id[:8]}",
        f"  developer: {shadow.developer or '?'}   tool call: {shadow.tool_use_id or '-'}",
    ]
    if shadow.target:
        out.append(f"  it was running: {shadow.target}")
    reasoning = origin.reasoning
    if reasoning.prompt:
        out += ["  prompt:", *_indent(reasoning.prompt)]
    elif shadow.prompt_excerpt:
        out.append(f"  prompt: {shadow.prompt_excerpt}")
    for note in reasoning.thinking:
        out += ["  thinking before the call:", *_indent(note)]
    if reasoning.decisions:
        out.append("  decisions that touched this file:")
        out += [f"    - {title}" for title in reasoning.decisions]
    return out


def _indent(text: str) -> list[str]:
    return [f"    {part}" for part in text.splitlines()[:12]]


def text_of_prompt(content: Any) -> str | None:
    """The text of a prompt event's content, shortened; None when empty."""
    text = text_of(content).strip()
    return text[:PROMPT_MAX] + ("..." if len(text) > PROMPT_MAX else "") if text else None
