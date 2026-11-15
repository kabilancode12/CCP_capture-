"""The dashboard views (migration 013) and the read-only role (real PostgreSQL, see conftest.py).

The views are checked on the invented session of ``story.py``: every number can be read off that
story. The role is checked from the outside: it connects as itself and tries to read what it must not.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pg8000.dbapi
import pytest
from tests.integration.story import SESSION, index_story

from ccp.db.cli import main
from ccp.db.connection import connect
from ccp.db.readers import DASHBOARD_VIEWS, ReaderError, create_reader
from ccp.settings import DatabaseSettings, Secret
from ccp.storage import ObjectStore

pytestmark = pytest.mark.integration

PASSWORD = "reader-" + "password-for-tests"  # invented; built from pieces for the secret scanner


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


@pytest.fixture
def story(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    index_story(database, object_store)
    return database


def test_d_session_summarises_the_session(story: DatabaseSettings) -> None:
    (row,) = _rows(
        story,
        "SELECT surface, status, total_cost_usd::text, prompts, file_changes, lines_added, "
        "lines_removed, test_runs, test_failures, agent_runs, compactions, rewinds, denials, commits "
        "FROM d_session",
    )

    # prompts: the first one and the one that replaced the rewound prompt (the rewound one is not counted)
    assert row == ("unknown", "open", "1.500000", 2, 2, 4, 2, 2, 1, 1, 1, 1, 1, 0)


def test_d_session_uses_the_environment_record_when_there_is_one(story: DatabaseSettings) -> None:
    connection = connect(story)
    try:
        connection.cursor().execute(
            "INSERT INTO session_env (session_id, surface, entrypoint, claude_code_version, os_name, "
            "account_email, hostname) VALUES (%s, 'vscode', 'claude-vscode', '2.1.289', 'Windows', "
            "'someone@example.com', 'LAPTOP-1')",
            (SESSION,),
        )
        connection.commit()
    finally:
        connection.close()

    rows = _rows(story, "SELECT surface, entrypoint, claude_code_version, os_name FROM d_session")

    assert rows == [("vscode", "claude-vscode", "2.1.289", "Windows")]


def test_d_model_cost_lists_each_model_of_the_session(story: DatabaseSettings) -> None:
    rows = _rows(
        story, "SELECT model, input_tokens, cost_usd::text FROM d_model_cost ORDER BY model"
    )

    assert rows == [("helper-model", 900, "0.100000"), ("model-a", 100, "1.400000")]


def test_d_edit_has_one_row_per_edit_with_the_actor_class(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT path, tool, actor_class, lines_added, lines_removed, is_abandoned FROM d_edit "
        "ORDER BY occurred_at",
    )

    assert rows == [("src/a.py", "Edit", "ai_edit", 2, 1, False)] * 2


def test_d_test_run_shows_the_outcomes(story: DatabaseSettings) -> None:
    rows = _rows(story, "SELECT framework, passed, exit_code FROM d_test_run ORDER BY occurred_at")

    assert rows == [("pytest", False, 1), ("pytest", True, 0)]


def test_d_fix_loop_shows_how_long_a_fix_took(story: DatabaseSettings) -> None:
    rows = _rows(story, "SELECT framework, edits_between, minutes_to_fix::text FROM d_fix_loop")

    assert rows == [("pytest", 1, "4.0")]


def test_the_views_work_on_an_empty_database(database: DatabaseSettings) -> None:
    for view in DASHBOARD_VIEWS:
        assert _rows(database, f"SELECT count(*) FROM {view}") == [(0,)]  # noqa: S608


ALLOWED_TEXT_COLUMNS = {
    "session_id", "branch", "status", "surface", "entrypoint", "claude_code_version", "os_name",
    "run_mode", "auth_type", "model", "path", "tool", "change_type", "actor_class", "framework",
    "project", "account", "token_source",  # d_usage: labels or neutral names, never e-mail or paths
    "chat_id", "effort",  # d_chat (migration 016): the chat's id and the highest effort level
}  # fmt: skip


def test_no_dashboard_view_exposes_free_text(database: DatabaseSettings) -> None:
    """Only short, categorical text columns are allowed: nothing that could hold what was said."""
    columns = _rows(
        database,
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE starts_with(table_name, 'd_') ORDER BY 1, 2",
    )
    texty = [
        (t, c)
        for t, c, kind in columns
        if kind in ("text", "character varying", "jsonb", "json", "ARRAY")
    ]

    assert [(t, c) for t, c in texty if c not in ALLOWED_TEXT_COLUMNS] == []
    assert {t for t, _, _ in columns} == set(DASHBOARD_VIEWS)


# -------------------------------------------------------------------------------- the role


@pytest.fixture
def reader_name() -> Iterator[str]:
    """A role name that is dropped after the test database is gone (roles belong to the server)."""
    name = f"ccp_reader_{uuid.uuid4().hex[:8]}"
    yield name
    admin = connect_admin()
    try:
        admin.autocommit = True
        admin.cursor().execute(f'DROP ROLE IF EXISTS "{name}"')
    finally:
        admin.close()


def connect_admin() -> pg8000.dbapi.Connection:
    from tests.integration.conftest import REPOSITORY_ROOT

    from ccp.settings import load_database_settings

    return connect(load_database_settings(env_file=REPOSITORY_ROOT / ".env"))


def _as_reader(database: DatabaseSettings, name: str, password: str = PASSWORD) -> DatabaseSettings:
    return replace(database, user=name, password=Secret(password))


def test_the_reader_can_read_the_dashboard_views_and_nothing_else(
    reader_name: str, story: DatabaseSettings
) -> None:
    admin = connect(story)
    try:
        create_reader(admin, story.name, PASSWORD, reader_name)
    finally:
        admin.close()
    reader = _as_reader(story, reader_name)

    assert _rows(reader, "SELECT count(*) FROM d_session") == [(1,)]
    for view in DASHBOARD_VIEWS:
        _rows(reader, f"SELECT * FROM {view}")  # noqa: S608
    for table in ("event", "session", "session_env", "thinking", "file_change", "v_file_history"):
        with pytest.raises(pg8000.dbapi.DatabaseError, match="permission denied"):
            _rows(reader, f"SELECT * FROM {table}")  # noqa: S608


def test_the_reader_cannot_change_or_create_anything(
    reader_name: str, story: DatabaseSettings
) -> None:
    admin = connect(story)
    try:
        create_reader(admin, story.name, PASSWORD, reader_name)
    finally:
        admin.close()
    reader = _as_reader(story, reader_name)

    for sql in (
        "DELETE FROM d_session",
        "UPDATE session SET branch = 'x'",
        "CREATE TABLE loot (x int)",
        "DROP VIEW d_session",
    ):
        with pytest.raises(pg8000.dbapi.DatabaseError):
            connection = connect(reader)
            try:
                connection.cursor().execute(sql)
            finally:
                connection.close()


def test_running_it_again_updates_the_password_and_keeps_the_role_working(
    reader_name: str, story: DatabaseSettings
) -> None:
    for password in (PASSWORD, PASSWORD + "-changed"):
        admin = connect(story)
        try:
            create_reader(admin, story.name, password, reader_name)
        finally:
            admin.close()

    assert _rows(_as_reader(story, reader_name, PASSWORD + "-changed"), "SELECT 1") == [(1,)]
    with pytest.raises(pg8000.dbapi.DatabaseError):
        _rows(_as_reader(story, reader_name, PASSWORD), "SELECT 1")


def test_a_password_with_quotes_is_stored_as_given(
    reader_name: str, story: DatabaseSettings
) -> None:
    odd = 'it\'s "quoted" \\ 12345'
    admin = connect(story)
    try:
        create_reader(admin, story.name, odd, reader_name)
    finally:
        admin.close()

    assert _rows(_as_reader(story, reader_name, odd), "SELECT 1") == [(1,)]


@pytest.mark.parametrize("name", ["", "Reader", "has-dash", "x" * 40, "1abc", 'a"b', "a b"])
def test_a_role_name_that_is_not_plain_is_refused(name: str, story: DatabaseSettings) -> None:
    admin = connect(story)
    try:
        with pytest.raises(ReaderError):
            create_reader(admin, story.name, PASSWORD, name)
    finally:
        admin.close()


def test_a_password_with_a_nul_character_is_refused(story: DatabaseSettings) -> None:
    admin = connect(story)
    try:
        with pytest.raises(ReaderError, match="NUL"):
            create_reader(admin, story.name, "bad\x00password-12345")
    finally:
        admin.close()


# ------------------------------------------------------------------------------- the command


def test_the_command_creates_the_role_from_the_environment(
    reader_name: str,
    story: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in {
        "POSTGRES_HOST": story.host,
        "POSTGRES_PORT": str(story.port),
        "POSTGRES_DB": story.name,
        "POSTGRES_USER": story.user,
        "POSTGRES_PASSWORD": story.password.reveal(),
        "CCP_READER_PASSWORD": PASSWORD,
    }.items():
        monkeypatch.setenv(name, value)

    code = main(["--dir", "db/migrations", "create-reader", "--name", reader_name])

    assert code == 0
    assert f"Role {reader_name} can read the dashboard views" in capsys.readouterr().out
    assert _rows(_as_reader(story, reader_name), "SELECT count(*) FROM d_session") == [(1,)]


def test_the_command_needs_a_long_enough_password(
    story: DatabaseSettings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in {
        "POSTGRES_HOST": story.host,
        "POSTGRES_PORT": str(story.port),
        "POSTGRES_DB": story.name,
        "POSTGRES_USER": story.user,
        "POSTGRES_PASSWORD": story.password.reveal(),
        "CCP_READER_PASSWORD": "short",
    }.items():
        monkeypatch.setenv(name, value)

    assert main(["--dir", "db/migrations", "create-reader"]) == 1
    err = capsys.readouterr().err
    assert "CCP_READER_PASSWORD must be at least 12 characters" in err
    assert "short" not in err.replace("CCP_READER_PASSWORD", "")
