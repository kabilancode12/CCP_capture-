"""Migration runner tests against a real PostgreSQL (see conftest.py)."""

from pathlib import Path
from typing import Any

import pytest

from ccp.db.migrations import (
    MigrationError,
    applied_versions,
    discover_migrations,
    migrate,
    rollback,
    status,
)

pytestmark = pytest.mark.integration

REAL_MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
STEP_1_TABLES = {"session", "session_env", "event", "redaction_log", "job"}
INSERT_EVENT = (
    "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, raw_line_number) "
)


def _tables(connection: Any) -> set[str]:
    cursor = connection.cursor()
    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    rows = cursor.fetchall()
    connection.commit()
    return {name for (name,) in rows}


def _make_migrations(folder: Path, **files: str) -> None:
    """Write migration files; keyword names use ``__`` for dots (001_a__sql -> 001_a.sql)."""
    for name, text in files.items():
        (folder / name.replace("__", ".")).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- the real schema


def test_real_schema_applies_creates_the_tables_and_rolls_back_cleanly(db_connection: Any) -> None:
    migrations = discover_migrations(REAL_MIGRATIONS)

    applied = migrate(db_connection, migrations)
    assert [m.label for m in applied] == ["001_initial_schema"]
    assert _tables(db_connection) >= STEP_1_TABLES

    undone = rollback(db_connection, migrations)
    assert [m.label for m in undone] == ["001_initial_schema"]
    assert _tables(db_connection) == {"schema_migration"}  # only the tracking table remains

    migrate(db_connection, migrations)  # and it can be applied again
    assert _tables(db_connection) >= STEP_1_TABLES


def test_real_schema_enforces_its_rules(db_connection: Any) -> None:
    migrate(db_connection, discover_migrations(REAL_MIGRATIONS))
    cursor = db_connection.cursor()
    cursor.execute("INSERT INTO session (session_id) VALUES ('s1')")
    # Allowed actors are limited to human / ai / subagent / system.
    with pytest.raises(Exception, match="actor"):
        cursor.execute(INSERT_EVENT + "VALUES ('s1', 'prompt', 'robot', '{}', 'k', 1)")


def test_the_same_transcript_line_cannot_become_two_events(db_connection: Any) -> None:
    migrate(db_connection, discover_migrations(REAL_MIGRATIONS))
    cursor = db_connection.cursor()
    cursor.execute("INSERT INTO session (session_id) VALUES ('s1')")
    insert = INSERT_EVENT + "VALUES ('s1', 'prompt', 'human', '{}', 'object-1', 7)"
    cursor.execute(insert)
    with pytest.raises(Exception, match="unique|duplicate"):
        cursor.execute(insert)


# ---------------------------------------------------------------- runner behaviour


def test_running_migrate_twice_applies_nothing_the_second_time(db_connection: Any) -> None:
    migrations = discover_migrations(REAL_MIGRATIONS)
    assert migrate(db_connection, migrations)
    assert migrate(db_connection, migrations) == []


def test_status_reports_applied_and_pending(db_connection: Any, tmp_path: Path) -> None:
    _make_migrations(
        tmp_path, **{"001_a__sql": "CREATE TABLE a (x int);", "002_b__sql": "SELECT 1;"}
    )
    migrations = discover_migrations(tmp_path)
    migrate(db_connection, migrations, target=1)
    assert [(m.label, done) for m, done in status(db_connection, migrations)] == [
        ("001_a", True),
        ("002_b", False),
    ]


def test_target_stops_after_the_given_version(db_connection: Any, tmp_path: Path) -> None:
    _make_migrations(
        tmp_path, **{"001_a__sql": "CREATE TABLE a (x int);", "002_b__sql": "SELECT 1;"}
    )
    applied = migrate(db_connection, discover_migrations(tmp_path), target=1)
    assert [m.version for m in applied] == [1]


def test_unknown_target_is_an_error(db_connection: Any) -> None:
    with pytest.raises(MigrationError, match="Unknown target"):
        migrate(db_connection, discover_migrations(REAL_MIGRATIONS), target=99)


def test_a_failing_migration_is_rolled_back_completely(db_connection: Any, tmp_path: Path) -> None:
    _make_migrations(
        tmp_path,
        **{
            "001_good__sql": "CREATE TABLE good (x int);",
            # The first statement succeeds, the second fails: neither may remain.
            "002_bad__sql": "CREATE TABLE half_done (x int); SELECT * FROM does_not_exist;",
        },
    )
    with pytest.raises(MigrationError, match="002_bad failed and was rolled back"):
        migrate(db_connection, discover_migrations(tmp_path))

    assert "half_done" not in _tables(db_connection)  # the partial change is gone
    assert "good" in _tables(db_connection)  # the earlier migration stays applied
    assert set(applied_versions(db_connection)) == {1}  # and only it is recorded


def test_an_edited_applied_migration_is_refused(db_connection: Any, tmp_path: Path) -> None:
    _make_migrations(tmp_path, **{"001_a__sql": "CREATE TABLE a (x int);"})
    migrate(db_connection, discover_migrations(tmp_path))

    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (y int);", encoding="utf-8")  # edited!
    with pytest.raises(MigrationError, match="changed after it was applied"):
        migrate(db_connection, discover_migrations(tmp_path))


def test_an_applied_migration_whose_file_is_gone_is_refused(
    db_connection: Any, tmp_path: Path
) -> None:
    _make_migrations(tmp_path, **{"001_a__sql": "SELECT 1;", "002_b__sql": "SELECT 1;"})
    migrate(db_connection, discover_migrations(tmp_path))
    (tmp_path / "002_b.sql").unlink()
    with pytest.raises(MigrationError, match="file is missing"):
        migrate(db_connection, discover_migrations(tmp_path))


def test_rollback_needs_a_down_file_and_undoes_nothing_without_one(
    db_connection: Any, tmp_path: Path
) -> None:
    _make_migrations(tmp_path, **{"001_a__sql": "CREATE TABLE a (x int);"})  # no down file
    migrations = discover_migrations(tmp_path)
    migrate(db_connection, migrations)
    with pytest.raises(MigrationError, match="no .down.sql"):
        rollback(db_connection, migrations)
    assert "a" in _tables(db_connection)


def test_rollback_steps_undo_the_newest_first(db_connection: Any, tmp_path: Path) -> None:
    _make_migrations(
        tmp_path,
        **{
            "001_a__sql": "CREATE TABLE a (x int);",
            "001_a__down__sql": "DROP TABLE a;",
            "002_b__sql": "CREATE TABLE b (x int);",
            "002_b__down__sql": "DROP TABLE b;",
        },
    )
    migrations = discover_migrations(tmp_path)
    migrate(db_connection, migrations)

    assert [m.label for m in rollback(db_connection, migrations, steps=1)] == ["002_b"]
    assert {"a"} <= _tables(db_connection) and "b" not in _tables(db_connection)
    assert [m.label for m in rollback(db_connection, migrations, steps=5)] == ["001_a"]
    assert "a" not in _tables(db_connection)
