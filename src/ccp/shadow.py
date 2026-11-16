"""A snapshot (a *shadow commit*) as the capture client reports it to the server.

Every change to a developer's working tree is recorded as one commit in the capture repository (see
``docs/snapshots.md``). The client tells the server about each one, so the index can link it to the
chat that caused it: which prompt, which tool call, which model, which files. Shared by the client
(which builds the record) and the server (which validates it); standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ACTORS = frozenset({"human", "ai_edit", "ai_command", "ai_other", "subagent"})
FILE_STATES = frozenset({"A", "M", "D"})
MAX_FILES = 500  # more files than this in one change are counted but not listed

_SHA = re.compile(r"[0-9a-f]{7,64}")
_TEXT_LIMITS = {
    "branch": 200, "chain": 300, "event": 40, "developer": 200, "model": 100, "tool": 60,
    "target": 200, "tool_use_id": 100, "prompt_uuid": 100, "prompt_excerpt": 400, "agent": 100,
}  # fmt: skip
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ShadowCommitError(ValueError):
    """Raised for a malformed record. The message never repeats the offending value."""


@dataclass(frozen=True)
class ShadowCommit:
    """One snapshot commit of the capture chain.

    Attributes:
        sha: The commit id in the capture repository.
        parent_sha: The previous commit of the chain (``None`` for the first, unless it hangs on
            the developer's HEAD, in which case that commit).
        actor: ``human``, ``ai_edit``, ``ai_command``, ``ai_other`` or ``subagent``.
        captured_at: When the snapshot was taken (the developer machine's clock).
        files: ``[{"path": ..., "state": "A" | "M" | "D"}]`` changed relative to the parent.
        files_total: How many files changed (may exceed the listed ones).
        seq: The snapshot's number within the session.
        fields: The optional text fields (branch, chain, event, developer, model, tool, target,
            tool_use_id, prompt_uuid, prompt_excerpt, agent).
    """

    sha: str
    actor: str
    captured_at: datetime
    parent_sha: str | None = None
    files: list[dict[str, str]] = field(default_factory=list)
    files_total: int = 0
    seq: int | None = None
    fields: dict[str, str] = field(default_factory=dict)


def parse_shadow_commit(data: Any) -> ShadowCommit:
    """Validate a decoded JSON object.

    Raises:
        ShadowCommitError: If it is not a valid record.
    """
    if not isinstance(data, dict):
        raise ShadowCommitError("a snapshot record must be a JSON object")
    sha = data.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ShadowCommitError("sha is not a commit id")
    parent = data.get("parent_sha")
    if parent is not None and (not isinstance(parent, str) or not _SHA.fullmatch(parent)):
        raise ShadowCommitError("parent_sha is not a commit id")
    actor = data.get("actor")
    if actor not in ACTORS:
        raise ShadowCommitError("actor is not known")
    try:
        at = datetime.fromisoformat(str(data.get("captured_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ShadowCommitError("captured_at is not a time") from None
    if at.tzinfo is None:
        raise ShadowCommitError("captured_at has no time zone")
    seq = data.get("seq")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
        raise ShadowCommitError("seq is not a number")
    fields: dict[str, str] = {}
    for name, limit in _TEXT_LIMITS.items():
        value = data.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit or _CONTROL.search(value):
            raise ShadowCommitError(f"{name} is not valid text")
        fields[name] = value
    files: list[dict[str, str]] = []
    raw_files = data.get("files", [])
    if not isinstance(raw_files, list):
        raise ShadowCommitError("files must be a list")
    for item in raw_files[:MAX_FILES]:
        path = item.get("path") if isinstance(item, dict) else None
        state = item.get("state") if isinstance(item, dict) else None
        if not isinstance(path, str) or not path or len(path) > 1000 or state not in FILE_STATES:
            raise ShadowCommitError("a file entry is not valid")
        files.append({"path": path, "state": state})
    total = data.get("files_total", len(files))
    if not isinstance(total, int) or isinstance(total, bool) or total < len(files):
        raise ShadowCommitError("files_total is not valid")
    return ShadowCommit(sha, actor, at, parent, files, total, seq, fields)
