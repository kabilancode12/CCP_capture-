"""Schema migrations: numbered SQL files applied in order and tracked in the database.

A migration is a file in ``db/migrations/``::

    001_initial_schema.sql         the change (required)
    001_initial_schema.down.sql    how to undo it (optional, needed for rollback)

Rules:

* Versions are three digits, start at 001 and have no gaps.
* **Applied files are never edited.** The checksum of each applied file is stored; if the file
  later changes, the runner refuses to continue. To change the schema, add a new migration.
* Each migration runs in its own transaction: it is applied completely or not at all.
* A database-wide lock stops two runners from migrating at the same time.
* Migration SQL must not contain a literal ``%`` (the driver treats it as a parameter marker);
  write ``%%`` instead.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pg8000.dbapi

# The table that records which migrations have been applied.
_CREATE_TRACKING_TABLE = """
    CREATE TABLE IF NOT EXISTS schema_migration (
        version    INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        checksum   TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""
_SELECT_APPLIED = "SELECT version, checksum FROM schema_migration"
_INSERT_APPLIED = "INSERT INTO schema_migration (version, name, checksum) VALUES (%s, %s, %s)"
_DELETE_APPLIED = "DELETE FROM schema_migration WHERE version = %s"

# Key of the PostgreSQL advisory lock held while migrating (any fixed number will do).
LOCK_KEY = 7_210_001

# 001_initial_schema.sql  or  001_initial_schema.down.sql
_FILE_PATTERN = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+?)(?P<down>\.down)?\.sql$")


class MigrationError(RuntimeError):
    """Raised when migration files are invalid or a migration cannot be applied."""


@dataclass(frozen=True)
class Migration:
    """One schema change.

    Attributes:
        version: Position in the sequence (1, 2, 3, ...).
        name: Short description taken from the file name.
        up_path: The SQL file that applies the change.
        down_path: The SQL file that undoes it, or ``None`` if the change cannot be undone.
    """

    version: int
    name: str
    up_path: Path
    down_path: Path | None

    @property
    def label(self) -> str:
        """The migration as written in file names and messages, for example ``001_initial``."""
        return f"{self.version:03d}_{self.name}"

    @property
    def checksum(self) -> str:
        """SHA-256 of the up file, with line endings normalised so Windows and Linux agree."""
        text = self.up_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- finding the files


def discover_migrations(directory: Path) -> list[Migration]:
    """Read and validate the migration files in ``directory``.

    Args:
        directory: Folder holding the ``NNN_name.sql`` files.

    Returns:
        The migrations in version order.

    Raises:
        MigrationError: If the folder is missing, a file name is malformed, a version is
            duplicated or missing, or a ``.down.sql`` file has no matching ``.sql`` file.
    """
    if not directory.is_dir():
        raise MigrationError(f"Migration folder not found: {directory}")

    ups: dict[int, tuple[str, Path]] = {}
    downs: dict[int, tuple[str, Path]] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_PATTERN.match(path.name)
        if match is None:
            raise MigrationError(f"Unexpected file name (want NNN_name.sql): {path.name}")
        version, name = int(match["version"]), match["name"]
        target = downs if match["down"] else ups
        if version in target:
            raise MigrationError(f"Duplicate migration version {version:03d}")
        target[version] = (name, path)

    for version, (name, _) in downs.items():
        if version not in ups or ups[version][0] != name:
            raise MigrationError(f"{version:03d}_{name}.down.sql has no matching up file")
    if sorted(ups) != list(range(1, len(ups) + 1)):
        raise MigrationError(
            f"Migration versions must run 001, 002, ... without gaps: {sorted(ups)}"
        )

    return [
        Migration(v, ups[v][0], ups[v][1], downs[v][1] if v in downs else None) for v in sorted(ups)
    ]


# ---------------------------------------------------------------- applying and undoing


@contextmanager
def _locked(connection: pg8000.dbapi.Connection) -> Iterator[None]:
    """Hold the migration lock; a second runner waits until the first has finished."""
    cursor = connection.cursor()
    cursor.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        yield
    finally:
        connection.rollback()  # leave no half-finished transaction behind
        cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        connection.commit()


def applied_versions(connection: pg8000.dbapi.Connection) -> dict[int, str]:
    """Return ``{version: checksum}`` of every applied migration, creating the table if needed."""
    cursor = connection.cursor()
    cursor.execute(_CREATE_TRACKING_TABLE)
    cursor.execute(_SELECT_APPLIED)
    rows = {int(version): str(checksum) for version, checksum in cursor.fetchall()}
    connection.commit()
    return rows


def _check_consistency(applied: dict[int, str], migrations: Sequence[Migration]) -> None:
    """Refuse to continue if the database and the files disagree about history."""
    by_version = {m.version: m for m in migrations}
    for version, checksum in applied.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(f"Version {version:03d} is applied but its file is missing")
        if migration.checksum != checksum:
            raise MigrationError(
                f"{migration.label} was changed after it was applied. "
                "Do not edit applied migrations; add a new one instead."
            )


def migrate(
    connection: pg8000.dbapi.Connection,
    migrations: Sequence[Migration],
    target: int | None = None,
) -> list[Migration]:
    """Apply every migration that has not been applied yet, in order.

    Args:
        connection: An open connection.
        migrations: All migrations, from :func:`discover_migrations`.
        target: Stop after this version. ``None`` applies everything.

    Returns:
        The migrations applied by this call (empty if the database was already up to date).

    Raises:
        MigrationError: On a changed or missing file, an unknown target, or a failing
            migration (which is rolled back completely; earlier ones stay applied).
    """
    if target is not None and target not in {m.version for m in migrations}:
        raise MigrationError(f"Unknown target version {target}")
    with _locked(connection):
        applied = applied_versions(connection)
        _check_consistency(applied, migrations)
        pending = [
            m
            for m in migrations
            if m.version not in applied and (target is None or m.version <= target)
        ]
        for migration in pending:
            _run(connection, migration, migration.up_path, record=True)
        return pending


def rollback(
    connection: pg8000.dbapi.Connection, migrations: Sequence[Migration], steps: int = 1
) -> list[Migration]:
    """Undo the most recently applied migrations, newest first.

    Args:
        connection: An open connection.
        migrations: All migrations, from :func:`discover_migrations`.
        steps: How many migrations to undo.

    Returns:
        The migrations undone by this call.

    Raises:
        MigrationError: If a migration to undo has no ``.down.sql`` file, or fails.
    """
    with _locked(connection):
        applied = applied_versions(connection)
        _check_consistency(applied, migrations)
        by_version = {m.version: m for m in migrations}
        to_undo = [by_version[v] for v in sorted(applied, reverse=True)[:steps]]
        plan: list[tuple[Migration, Path]] = []
        for migration in to_undo:  # check first, so nothing is undone if one cannot be
            if migration.down_path is None:
                raise MigrationError(f"{migration.label} has no .down.sql file; cannot roll back")
            plan.append((migration, migration.down_path))
        for migration, down_path in plan:
            _run(connection, migration, down_path, record=False)
        return to_undo


def status(
    connection: pg8000.dbapi.Connection, migrations: Sequence[Migration]
) -> list[tuple[Migration, bool]]:
    """Return every migration with ``True`` if it has been applied."""
    applied = applied_versions(connection)
    _check_consistency(applied, migrations)
    return [(m, m.version in applied) for m in migrations]


def _run(
    connection: pg8000.dbapi.Connection, migration: Migration, path: Path, *, record: bool
) -> None:
    """Run one file in its own transaction and update the tracking table.

    Args:
        connection: An open connection.
        migration: The migration being applied or undone.
        path: The SQL file to run.
        record: True after applying (insert a tracking row), False after undoing (delete it).
    """
    cursor = connection.cursor()
    try:
        cursor.execute(path.read_text(encoding="utf-8"))
        if record:
            cursor.execute(_INSERT_APPLIED, (migration.version, migration.name, migration.checksum))
        else:
            cursor.execute(_DELETE_APPLIED, (migration.version,))
        connection.commit()
    except Exception as error:
        connection.rollback()
        raise MigrationError(f"{migration.label} failed and was rolled back: {error}") from error
