"""Search, show, compare and restore approaches (a decision with its code).

Reads the index and the capture repository; restoring writes files, and records the reuse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ccp.approaches.lineage import same_file
from ccp.audit.gitlog import _git

TEXT_COLUMNS = "d.title, d.goal, d.chosen, d.reason, d.result, d.options::text, d.files::text"
_COLUMNS = """
    d.decision_id, d.session_id, d.seq, d.title, d.goal, d.options, d.chosen, d.reason, d.result,
    d.tests_summary, d.decided_by, d.model, d.status, d.lifecycle, d.code_sha, d.removed_in_sha,
    d.removed_at, d.files, d.thinking, d.created_at,
    (SELECT r.title FROM decision r WHERE r.decision_id = d.removed_by_decision_id) AS removed_by,
    d.removed_by_decision_id,
    (SELECT s.title FROM decision s WHERE s.decision_id = d.supersedes_decision_id) AS supersedes,
    d.supersedes_decision_id,
    COALESCE(c.project_label, c.project_name) AS project, c.title AS chat_title,
    COALESCE(c.account_label, c.account_name) AS account,
    (SELECT count(*) FROM approach_reuse u WHERE u.decision_id = d.decision_id) AS reuses
"""
_FROM = "FROM decision d LEFT JOIN chat c ON c.chat_id = d.session_id"
_NAMES = [
    "decision_id", "session_id", "seq", "title", "goal", "options", "chosen", "reason", "result",
    "tests_summary", "decided_by", "model", "status", "lifecycle", "code_sha", "removed_in_sha",
    "removed_at", "files", "thinking", "created_at", "removed_by", "removed_by_id", "supersedes",
    "supersedes_id", "project", "chat_title", "account", "reuses",
]  # fmt: skip


@dataclass(frozen=True)
class Approach:
    """A decision with where its code is and what became of it."""

    decision_id: int
    session_id: str
    seq: int
    title: str | None
    goal: str | None
    options: list[Any] | None
    chosen: str | None
    reason: str | None
    result: str | None
    tests_summary: str | None
    decided_by: str | None
    model: str | None
    status: str
    lifecycle: str
    code_sha: str | None
    removed_in_sha: str | None
    removed_at: datetime | None
    files: list[Any] | None
    thinking: list[Any] | None
    created_at: datetime
    removed_by: str | None
    removed_by_id: int | None
    supersedes: str | None
    supersedes_id: int | None
    project: str | None
    chat_title: str | None
    account: str | None
    reuses: int
    reuse_log: list[dict[str, Any]] = field(default_factory=list)

    def file_paths(self) -> list[tuple[str, str]]:
        """``(path, change)`` of the files the decision created or modified."""
        return [
            (str(f["path"]), str(f.get("change")))
            for f in self.files or []
            if isinstance(f, dict) and f.get("path") and f.get("change") in ("created", "modified")
        ]


def _approach(row: tuple[Any, ...]) -> Approach:
    values = dict(zip(_NAMES, row, strict=True))
    values["reuses"] = int(values["reuses"] or 0)
    values.pop("removed_by_id", None)
    return Approach(removed_by_id=row[_NAMES.index("removed_by_id")], **values)


def search(
    connection: Any, text: str, *, only_removed: bool = False, limit: int = 20
) -> list[Approach]:
    """Approaches whose text contains every word of ``text``, best title matches first."""
    words = [w for w in text.split() if w]
    cursor = connection.cursor()
    patterns = [f"%{w}%" for w in words] or ["%"]
    where = [f"(concat_ws(' ', {TEXT_COLUMNS})) ILIKE ALL (%s)", "d.status <> 'rejected'"]
    if only_removed:
        where.append("d.lifecycle <> 'active'")
    cursor.execute(
        f"SELECT {_COLUMNS} {_FROM} WHERE {' AND '.join(where)} "  # noqa: S608  (fixed text)
        "ORDER BY (d.title ILIKE ALL (%s)) DESC, d.lifecycle = 'active', "
        "d.created_at DESC LIMIT %s",
        (patterns, patterns, limit),
    )
    return [_approach(row) for row in cursor.fetchall()]


def get(connection: Any, decision_id: int) -> Approach | None:
    """One approach with its reuse log, or None."""
    cursor = connection.cursor()
    cursor.execute(f"SELECT {_COLUMNS} {_FROM} WHERE d.decision_id = %s", (decision_id,))  # noqa: S608
    row = cursor.fetchone()
    if row is None:
        return None
    item = _approach(tuple(row))
    cursor.execute(
        "SELECT reused_by, purpose, target, reused_at FROM approach_reuse "
        "WHERE decision_id = %s ORDER BY reused_at",
        (decision_id,),
    )
    log = [
        {"reused_by": r[0], "purpose": r[1], "target": r[2], "reused_at": r[3]}
        for r in cursor.fetchall()
    ]
    return Approach(**{**item.__dict__, "reuse_log": log})


class RestoreError(RuntimeError):
    """Raised when an approach cannot be restored."""


def restore(
    connection: Any,
    repository: Path,
    approach: Approach,
    into: Path,
    *,
    by: str,
    purpose: str,
    force: bool = False,
) -> list[Path]:
    """Write the approach's files, as they were, under ``into``, and record the reuse.

    Args:
        connection: Open connection (the reuse is added; the caller commits).
        repository: A clone of the capture project holding the approach's code commit.
        approach: The approach to restore.
        into: The folder to write into (created if needed).
        by: Who restores it.
        purpose: What it is used for.
        force: Overwrite files that already exist.

    Raises:
        RestoreError: If the code is not known, a file is not in it, or a file exists.
    """
    if not approach.code_sha:
        raise RestoreError("this decision has no code on the capture chain")
    tree = _git(repository, "ls-tree", "-r", "--name-only", approach.code_sha).splitlines()
    plan: list[tuple[str, Path]] = []
    for path, _ in approach.file_paths():
        clean = path.replace("\\", "/").strip("/")
        source = next((t for t in tree if same_file(t, clean)), None)
        if source is None:
            continue  # a folder or a file that is not in that commit
        relative = clean if not path.startswith("/") else source.rsplit("/", 1)[-1]
        if ".." in relative.split("/"):
            raise RestoreError("a file name tries to leave the target folder")
        plan.append((source, into / relative))
    if not plan:
        raise RestoreError("none of the approach's files is in its code commit")
    existing = [t for _, t in plan if t.exists()]
    if existing and not force:
        raise RestoreError(f"{existing[0]} exists; use --force to overwrite")
    written: list[Path] = []
    for source, target in plan:
        content = _git(repository, "show", f"{approach.code_sha}:{source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        written.append(target)
    connection.cursor().execute(
        "INSERT INTO approach_reuse "
        "(decision_id, approach_title, code_sha, reused_by, purpose, target) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (approach.decision_id, approach.title, approach.code_sha, by, purpose, str(into)),
    )
    return written


def diff(repository: Path, first: Approach, second: Approach, limit: int = 200) -> list[str]:
    """The code difference between two approaches' commits, for the files either one lists."""
    if not first.code_sha or not second.code_sha:
        return []
    paths = sorted({p for a in (first, second) for p, _ in a.file_paths() if "/" in p or "." in p})
    out = _git(repository, "diff", "--no-color", first.code_sha, second.code_sha, "--", *paths)
    return out.splitlines()[:limit]
