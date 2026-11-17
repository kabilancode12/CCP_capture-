"""Where a decision's code is, and whether a later decision removed it.

Rules, all on recorded facts (no model involved):

* A decision's **code** is the newest capture commit of its own chat that added or changed one of
  the files the decision lists. That commit's tree holds the decision's files as they were.
* The decision's code was **removed** when, later, any capture commit of the same project deleted a
  file the decision created. That commit is ``removed_in_sha``.
* The decision that **removed** it is the one, in the chat of the removing commit, that lists that
  file as deleted. That decision **supersedes** the removed one; the removed one is then
  ``superseded`` (otherwise ``removed``, or ``active`` while its files still exist).

File names in decisions are relative to the chat's folder and in commits relative to the repository
root, so two names match when one ends with the other on a folder boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

_REAL = ("created", "modified")


@dataclass(frozen=True)
class Shadow:
    """A capture commit, as far as lineage needs it."""

    sha: str
    session_id: str
    captured_at: datetime
    files: tuple[tuple[str, str], ...]  # (path, state A/M/D)
    project_id: int | None


@dataclass(frozen=True)
class DecisionFiles:
    """A decision, as far as lineage needs it."""

    decision_id: int
    session_id: str
    seq: int
    project_id: int | None
    files: tuple[tuple[str, str], ...]  # (path, created/modified/deleted)


@dataclass(frozen=True)
class Lineage:
    """The result for one decision."""

    code_sha: str | None = None
    removed_in_sha: str | None = None
    removed_at: datetime | None = None
    removed_by: int | None = None
    lifecycle: str = "active"


def same_file(a: str, b: str) -> bool:
    """True when two paths name the same file (equal, or one ends with the other)."""
    a, b = a.replace("\\", "/").strip("/"), b.replace("\\", "/").strip("/")
    return bool(a and b) and (a == b or a.endswith("/" + b) or b.endswith("/" + a))


def _touches(shadow: Shadow, paths: list[str], states: tuple[str, ...]) -> list[str]:
    return [p for p in paths if any(s in states and same_file(f, p) for f, s in shadow.files)]


def compute(decisions: list[DecisionFiles], shadows: list[Shadow]) -> dict[int, Lineage]:
    """Work out the lineage of every decision that lists files.

    Returns:
        ``{decision_id: Lineage}``; decisions without a recorded file get no entry.
    """
    ordered = sorted(shadows, key=lambda s: s.captured_at)
    result: dict[int, Lineage] = {}
    for d in decisions:
        paths = [p for p, change in d.files if change in _REAL]
        created = [p for p, change in d.files if change == "created"]
        own = [
            s for s in ordered if s.session_id == d.session_id and _touches(s, paths, ("A", "M"))
        ]
        if not own:
            continue
        code = own[-1]
        removal = next(
            (
                s
                for s in ordered
                if s.captured_at > code.captured_at
                and s.project_id == d.project_id
                and _touches(s, created, ("D",))
            ),
            None,
        )
        if removal is None:
            result[d.decision_id] = Lineage(code_sha=code.sha)
            continue
        gone = _touches(removal, created, ("D",))
        remover = next(
            (
                other
                for other in sorted(decisions, key=lambda x: x.seq)
                if other.decision_id != d.decision_id
                and other.session_id == removal.session_id
                and any(ch == "deleted" and same_file(p, g) for p, ch in other.files for g in gone)
            ),
            None,
        )
        result[d.decision_id] = Lineage(
            code.sha,
            removal.sha,
            removal.captured_at,
            remover.decision_id if remover else None,
            "superseded" if remover else "removed",
        )
    return result


_DECISIONS = """
    SELECT d.decision_id, d.session_id, d.seq, c.project_id, d.files
      FROM decision d LEFT JOIN chat c ON c.chat_id = d.session_id
     WHERE d.status <> 'rejected'
"""
_SHADOWS = """
    SELECT s.sha, s.session_id, s.captured_at, s.files, c.project_id
      FROM shadow_commit s LEFT JOIN chat c ON c.chat_id = s.session_id
"""
_RESET = """
    UPDATE decision SET lifecycle = 'active', code_sha = NULL, removed_in_sha = NULL,
           removed_at = NULL, removed_by_decision_id = NULL, supersedes_decision_id = NULL
"""
_SET = """
    UPDATE decision SET lifecycle = %s, code_sha = %s, removed_in_sha = %s, removed_at = %s,
           removed_by_decision_id = %s WHERE decision_id = %s
"""
_SUPERSEDES = "UPDATE decision SET supersedes_decision_id = %s WHERE decision_id = %s"


def _pairs(value: Any, key: str, state_key: str) -> tuple[tuple[str, str], ...]:
    items = value if isinstance(value, list) else []
    return tuple(
        (str(i[key]), str(i.get(state_key) or ""))
        for i in items
        if isinstance(i, dict) and isinstance(i.get(key), str)
    )


def refresh_lineage(connection: Any) -> int:
    """Recompute the lineage of every decision and store it (in the caller's transaction).

    Returns:
        How many decisions have code on the capture chain.
    """
    cursor = connection.cursor()
    cursor.execute(_DECISIONS)
    decisions = [
        DecisionFiles(r[0], r[1], r[2], r[3], _pairs(r[4], "path", "change"))
        for r in cursor.fetchall()
    ]
    cursor.execute(_SHADOWS)
    shadows = [
        Shadow(r[0], r[1], r[2], _pairs(r[3], "path", "state"), r[4]) for r in cursor.fetchall()
    ]
    found = compute(decisions, shadows)
    cursor.execute(_RESET)
    for decision_id, item in found.items():
        cursor.execute(
            _SET,
            (
                item.lifecycle,
                item.code_sha,
                item.removed_in_sha,
                item.removed_at,
                item.removed_by,
                decision_id,
            ),
        )
        if item.removed_by is not None:
            cursor.execute(_SUPERSEDES, (decision_id, item.removed_by))
    return len(found)
