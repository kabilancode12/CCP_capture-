"""Migration 016 moves what exists into the chat hierarchy, and its rollback brings it back.

Before 016 an account was known by its e-mail address (``session_env``), a project by its folder (the
``cwd`` of the transcript), and labels lived in ``account_label`` and ``project_label``.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pg8000.dbapi
import pytest
from tests.integration.conftest import REAL_MIGRATIONS, REPOSITORY_ROOT

from ccp.db.connection import connect
from ccp.db.hierarchy import link_chat
from ccp.db.migrations import discover_migrations, migrate, rollback
from ccp.settings import DatabaseSettings, Secret, load_database_settings

pytestmark = pytest.mark.integration

EMAIL = "dev@" + "example.com"
READER_PASSWORD = "reader-" + "test-password-1"  # invented


def _rows(connection: Any, sql: str) -> list[tuple[Any, ...]]:
    cursor = connection.cursor()
    cursor.execute(sql)
    return [tuple(row) for row in cursor.fetchall()]


def _run(connection: Any, *statements: str) -> None:
    cursor = connection.cursor()
    for statement in statements:
        cursor.execute(statement)
    connection.commit()


@pytest.fixture
def at_015(db_connection: Any) -> Any:
    """A database at migration 015 with two chats, their environment, events and labels."""
    migrate(db_connection, discover_migrations(REAL_MIGRATIONS), target=15)
    _run(
        db_connection,
        "INSERT INTO session (session_id, started_at) VALUES ('c1', '2026-11-14 10:00+00'), "
        "('c2', '2026-11-14 11:00+00'), ('c3', NULL)",
        f"INSERT INTO session_env (session_id, account_email, account_name, hostname) VALUES "  # noqa: S608
        f"('c1', '{EMAIL.upper()}', 'Dev', 'Laptop-1'), ('c2', '{EMAIL}', 'Dev', 'Laptop-1'), "
        f"('c3', 'other@example.com', NULL, NULL)",
        "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, raw_line_number) "
        "VALUES ('c1', 'prompt', 'human', '{\"cwd\": \"/home/dev/ccp_V2\"}', "
        "'sessions/c1/main/0.jsonl', 1), "
        "('c2', 'prompt', 'human', '{\"cwd\": \"/home/dev/ccp_V2\"}', "
        "'sessions/c2/main/0.jsonl', 1)",
        f"INSERT INTO account_label VALUES ('{EMAIL}', 'claude1')",  # noqa: S608  (test data)
        "INSERT INTO project_label VALUES ('/home/dev/ccp_V2', 'capture'), "
        "('/srv/never-seen', 'ghost')",
    )
    return db_connection


def test_accounts_projects_and_labels_are_moved(at_015: Any) -> None:
    migrate(at_015, discover_migrations(REAL_MIGRATIONS))

    assert _rows(at_015, "SELECT email, display_name, label FROM claude_account ORDER BY 1") == [
        (EMAIL, "Dev", "claude1"),
        ("other@example.com", None, None),
    ]
    assert _rows(
        at_015, "SELECT project_key, key_kind, name, folders, label FROM project ORDER BY name"
    ) == [
        ("laptop-1:/home/dev/ccp_V2", "folder", "ccp_V2", ["/home/dev/ccp_V2"], "capture"),
        ("/srv/never-seen", "folder", "never-seen", ["/srv/never-seen"], "ghost"),
    ]
    assert _rows(
        at_015,
        "SELECT s.session_id, a.label, p.label, env.folder FROM session s "
        "LEFT JOIN claude_account a USING (account_id) LEFT JOIN project p USING (project_id) "
        "LEFT JOIN session_env env USING (session_id) ORDER BY 1",
    ) == [
        ("c1", "claude1", "capture", "/home/dev/ccp_V2"),
        ("c2", "claude1", "capture", "/home/dev/ccp_V2"),
        ("c3", None, None, None),
    ]
    tables = {row[0] for row in _rows(at_015, "SELECT tablename FROM pg_tables")}
    assert not tables & {"account_label", "project_label"}


def test_the_rollback_restores_the_label_tables_and_the_old_usage_view(at_015: Any) -> None:
    migrations = discover_migrations(REAL_MIGRATIONS)
    migrate(at_015, migrations)
    _run(at_015, "UPDATE project SET label = 'renamed' WHERE name = 'ccp_V2'")

    rollback(at_015, migrations, steps=1)

    assert _rows(at_015, "SELECT * FROM account_label") == [(EMAIL, "claude1")]
    assert _rows(at_015, "SELECT * FROM project_label ORDER BY 1") == [
        ("/home/dev/ccp_V2", "renamed"),
        ("/srv/never-seen", "ghost"),
    ]
    assert _rows(at_015, "SELECT count(*) FROM d_usage") == [(0,)]
    tables = {row[0] for row in _rows(at_015, "SELECT tablename FROM pg_tables")}
    assert not tables & {"claude_account", "project", "chat_session", "decision", "ui_user"}

    migrate(at_015, migrations)  # and forward again
    assert _rows(at_015, "SELECT count(*) FROM claude_account") == [(2,)]


def test_a_folder_labelled_before_any_chat_keeps_its_label_when_a_chat_arrives(
    at_015: Any,
) -> None:
    migrate(at_015, discover_migrations(REAL_MIGRATIONS))
    _run(
        at_015,
        "INSERT INTO session (session_id) VALUES ('c4')",
        "INSERT INTO session_env (session_id, hostname, folder) "
        "VALUES ('c4', 'Laptop-1', '/srv/never-seen')",
    )
    cursor = at_015.cursor()
    link_chat(cursor, "c4")
    at_015.commit()

    assert _rows(at_015, "SELECT project_label FROM chat WHERE chat_id = 'c4'") == [("ghost",)]
    assert _rows(at_015, "SELECT project_key FROM project WHERE name = 'never-seen'") == [
        ("laptop-1:/srv/never-seen",)
    ]


def test_a_windows_folder_is_one_project_whatever_the_drive_letter_case(
    db_connection: Any,
) -> None:
    migrate(db_connection, discover_migrations(REAL_MIGRATIONS), target=15)
    _run(
        db_connection,
        "INSERT INTO session (session_id) VALUES ('w1'), ('w2')",
        "INSERT INTO session_env (session_id, hostname) VALUES ('w1', 'Box'), ('w2', 'Box')",
        "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, raw_line_number) "
        "VALUES ('w1', 'prompt', 'human', '{\"cwd\": \"C:\\\\work\\\\app\"}', "
        "'sessions/w1/main/0.jsonl', 1), "
        "('w2', 'prompt', 'human', '{\"cwd\": \"c:\\\\work\\\\app\"}', "
        "'sessions/w2/main/0.jsonl', 1)",
    )

    migrate(db_connection, discover_migrations(REAL_MIGRATIONS))

    ((key, name, folders),) = _rows(db_connection, "SELECT project_key, name, folders FROM project")
    assert (key, name, sorted(folders)) == (
        "box:c:\\work\\app",
        "app",
        ["C:\\work\\app", "c:\\work\\app"],
    )
    assert _rows(db_connection, "SELECT count(DISTINCT project_id) FROM session") == [(1,)]


def test_a_host_name_with_spaces_gives_the_key_link_chat_gives(db_connection: Any) -> None:
    migrate(db_connection, discover_migrations(REAL_MIGRATIONS), target=15)
    _run(
        db_connection,
        "INSERT INTO session (session_id) VALUES ('h1'), ('h2')",
        "INSERT INTO session_env (session_id, hostname) VALUES ('h1', ' Box '), ('h2', '  ')",
        "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, raw_line_number) "
        "VALUES ('h1', 'prompt', 'human', '{\"cwd\": \"/srv/app\"}', 'sessions/h1/main/0.jsonl', 1), "
        "('h2', 'prompt', 'human', '{\"cwd\": \"/srv/other\"}', 'sessions/h2/main/0.jsonl', 1)",
    )

    migrate(db_connection, discover_migrations(REAL_MIGRATIONS))
    link_chat(db_connection.cursor(), "h1")  # the same key: nothing new
    db_connection.commit()

    assert set(_rows(db_connection, "SELECT project_key FROM project")) == {
        ("/srv/other",),
        ("box:/srv/app",),
    }


# ---------------------------------------------------------------- the read-only roles


@pytest.fixture
def reader_role() -> Iterator[str]:
    """A login role that is dropped after the test database is gone (roles belong to the server)."""
    name = f"ccp_reader_{uuid.uuid4().hex[:8]}"
    yield name
    admin = connect(load_database_settings(env_file=REPOSITORY_ROOT / ".env"))
    try:
        admin.autocommit = True
        admin.cursor().execute(f'DROP ROLE IF EXISTS "{name}"')
    finally:
        admin.close()


def _can_read(database: DatabaseSettings, role: str, view: str) -> bool:
    connection = connect(replace(database, user=role, password=Secret(READER_PASSWORD)))
    try:
        connection.cursor().execute(f"SELECT * FROM {view}")  # noqa: S608  (fixed view names)
        return True
    except pg8000.dbapi.DatabaseError as error:
        assert "permission denied" in str(error)
        return False
    finally:
        connection.close()


def test_a_reader_of_d_usage_keeps_it_and_can_read_d_chat(
    reader_role: str, temp_database: DatabaseSettings
) -> None:
    # What `ccp-db create-reader` did at migration 015: the reader may read the dashboard views.
    connection = connect(temp_database)
    try:
        migrate(connection, discover_migrations(REAL_MIGRATIONS), target=15)
        _run(
            connection,
            f"CREATE ROLE \"{reader_role}\" LOGIN PASSWORD '{READER_PASSWORD}'",
            f'GRANT CONNECT ON DATABASE "{temp_database.name}" TO "{reader_role}"',
            f'GRANT USAGE ON SCHEMA public TO "{reader_role}"',
            f'GRANT SELECT ON d_usage TO "{reader_role}"',
        )
        assert _can_read(temp_database, reader_role, "d_usage")

        migrate(connection, discover_migrations(REAL_MIGRATIONS))
        assert _can_read(temp_database, reader_role, "d_usage")
        assert _can_read(temp_database, reader_role, "d_chat")
        assert not _can_read(temp_database, reader_role, "chat")  # not a dashboard view

        rollback(connection, discover_migrations(REAL_MIGRATIONS), steps=1)
        assert _can_read(temp_database, reader_role, "d_usage")
    finally:
        connection.close()
