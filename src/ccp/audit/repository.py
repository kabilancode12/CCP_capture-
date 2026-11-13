"""The database queries behind ``ccp-audit``. Read-only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pg8000.dbapi

from ccp.audit.provenance import Edit, Hunk
from ccp.db.connection import connect
from ccp.settings import DatabaseSettings


class AuditError(RuntimeError):
    """Raised when the database cannot be read or the question cannot be answered."""


class AmbiguousPathError(AuditError):
    """The same relative path was edited in several places (for example in two projects)."""

    def __init__(self, path: str, candidates: list[str]) -> None:
        """Remember the locations found, and say what to do about it."""
        self.candidates = candidates
        super().__init__(
            f"{path!r} was edited at more than one location; give the full path of one of: "
            + ", ".join(candidates)
        )


# Every recorded change to a path, oldest first, with what the replay needs: the patch (and, for a
# new file, its content) are read from the stored result entry. Rewound (abandoned) work is left
# out unless asked for.
_FILE_EDITS = """
    SELECT f.file_change_id, f.session_id, e.occurred_at, f.tool, f.user_modified, f.absolute_path,
           f.lines_added, f.lines_removed, e.is_abandoned,
           e.payload #> '{toolUseResult,structuredPatch}',
           CASE WHEN e.payload #>> '{toolUseResult,type}' = 'create'
                THEN e.payload #>> '{toolUseResult,content}' END,
           left(CASE WHEN jsonb_typeof(p.payload #> '{message,content}') = 'string'
                     THEN p.payload #>> '{message,content}'
                     ELSE jsonb_path_query_first(
                              p.payload, '$.message.content[*] ? (@.type == "text").text') #>> '{}'
                END, 500)
      FROM file_change f
      JOIN event e ON e.event_id = f.event_id
      LEFT JOIN event p ON p.event_id = f.prompt_event_id
     WHERE (f.path = %s OR replace(f.absolute_path, chr(92), '/') = %s)
       AND (%s OR NOT e.is_abandoned)
     ORDER BY e.occurred_at NULLS LAST, f.file_change_id
"""


@dataclass(frozen=True)
class RecordedEdit:
    """One edit of a file with the facts a person wants next to it."""

    edit: Edit  # for the replay
    session_id: str
    occurred_at: datetime | None
    tool: str | None
    user_modified: bool | None
    absolute_path: str
    lines_added: int
    lines_removed: int
    is_abandoned: bool
    prompt: str | None


class AuditRepository:
    """Read-only access to the index."""

    def __init__(self, settings: DatabaseSettings) -> None:
        """Remember the connection settings; nothing is connected yet."""
        self._settings = settings

    def file_edits(self, path: str, *, include_abandoned: bool = False) -> list[RecordedEdit]:
        """Every recorded edit of a file, oldest first.

        Args:
            path: A path relative to the session folder, or an absolute path (either separator).
            include_abandoned: Also list edits made on rewound branches.

        Raises:
            AmbiguousPathError: If the path matches files in more than one location.
            AuditError: If the database cannot be read.
        """
        wanted = path.replace("\\", "/")
        rows = self._query(_FILE_EDITS, (wanted, wanted, include_abandoned))
        locations = sorted({str(row[5]) for row in rows})
        if len(locations) > 1:
            raise AmbiguousPathError(path, locations)
        return [_recorded(row) for row in rows]

    def _query(self, sql: str, parameters: tuple[object, ...]) -> list[tuple[Any, ...]]:
        try:
            connection = connect(self._settings)
        except (pg8000.dbapi.Error, OSError) as error:
            raise AuditError(f"database connection failed: {type(error).__name__}") from error
        try:
            cursor = connection.cursor()
            cursor.execute(sql, parameters)
            return [tuple(row) for row in cursor.fetchall()]
        except (pg8000.dbapi.Error, OSError) as error:
            raise AuditError(f"a query failed: {type(error).__name__}") from error
        finally:
            connection.close()


def _recorded(row: tuple[Any, ...]) -> RecordedEdit:
    """Build a :class:`RecordedEdit` from a query row."""
    change_id, session_id, when, tool, modified, absolute, added, removed, abandoned = row[:9]
    patch, content, prompt = row[9], row[10], row[11]
    created = len(content.splitlines()) if isinstance(content, str) else None
    return RecordedEdit(
        edit=Edit(change_id=int(change_id), hunks=_hunks(patch), created_lines=created),
        session_id=str(session_id),
        occurred_at=when,
        tool=tool,
        user_modified=modified,
        absolute_path=str(absolute),
        lines_added=int(added),
        lines_removed=int(removed),
        is_abandoned=bool(abandoned),
        prompt=prompt,
    )


def _hunks(patch: object) -> tuple[Hunk, ...]:
    """The hunks of a stored ``structuredPatch``; anything malformed is left out."""
    if not isinstance(patch, list):
        return ()
    hunks = []
    for item in patch:
        if not isinstance(item, dict):
            continue
        start, count, lines = item.get("oldStart"), item.get("oldLines"), item.get("lines")
        if not (isinstance(start, int) and isinstance(count, int) and isinstance(lines, list)):
            continue
        hunks.append(Hunk(start, count, tuple(line for line in lines if isinstance(line, str))))
    return tuple(hunks)
