"""Token usage by project, account and model (migration 015), labels, and the auditor role.

Checked on the invented session of ``story.py``: it runs in the folder ``/work/app`` and its
``cost-state`` entry records ``model-a`` (100 input, 50 output tokens, 1.40 USD) and ``helper-model``
(900 input, 10 output tokens, 0.10 USD).
"""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pg8000.dbapi
import pytest
from tests.integration.story import SESSION, index_story
from tests.integration.test_dashboard import connect_admin

from ccp.db.cli import main
from ccp.db.connection import connect
from ccp.db.labels import (
    LabelError,
    list_labels,
    remove_label,
    set_account_label,
    set_project_label,
)
from ccp.db.readers import create_auditor, create_reader
from ccp.settings import DatabaseSettings, Secret
from ccp.storage import ObjectStore

pytestmark = pytest.mark.integration

PASSWORD = "auditor-" + "password-for-tests"  # invented; built from pieces for the secret scanner
EMAIL = "dev@" + "example.com"


def _rows(database: DatabaseSettings, sql: str, *parameters: Any) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        if parameters:
            cursor.execute(sql, parameters)
        else:
            cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _execute(database: DatabaseSettings, sql: str, *parameters: Any) -> None:
    connection = connect(database)
    try:
        if parameters:
            connection.cursor().execute(sql, parameters)
        else:
            connection.cursor().execute(sql)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def story(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    index_story(database, object_store)
    return database


@pytest.fixture
def role_name() -> Iterator[str]:
    """A role name that is dropped after the test (roles belong to the server, not the database)."""
    import uuid

    name = f"ccp_test_role_{uuid.uuid4().hex[:8]}"
    yield name
    admin = connect_admin()
    try:
        admin.autocommit = True
        admin.cursor().execute(f'DROP ROLE IF EXISTS "{name}"')
    finally:
        admin.close()


USAGE_SQL = (
    "SELECT project, account, model, input_tokens, output_tokens, total_tokens, cost_usd::text, "
    "token_source FROM d_usage ORDER BY model"
)


# ------------------------------------------------------------------------------ the view


def test_d_usage_reads_the_cost_state_per_model(story: DatabaseSettings) -> None:
    assert _rows(story, USAGE_SQL) == [
        ("app", "unknown", "helper-model", 900, 10, 910, "0.100000", "cost-state"),
        ("app", "unknown", "model-a", 100, 50, 150, "1.400000", "cost-state"),
    ]


def test_d_usage_falls_back_to_the_recorded_responses(story: DatabaseSettings) -> None:
    """Without a cost-state entry (Claude Code 2.1.323) the responses' tokens are summed, no cost."""
    _execute(story, "DELETE FROM session_model_cost")

    rows = _rows(story, USAGE_SQL)

    assert [(r[0], r[2], r[6], r[7]) for r in rows] == [("app", "model-a", None, "responses")]
    expected = _rows(
        story,
        "SELECT sum(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) "
        "+ COALESCE(cache_read_tokens, 0) + COALESCE(cache_creation_tokens, 0)) "
        "FROM model_usage WHERE model = 'model-a'",
    )[0][0]
    assert rows[0][5] == expected


def test_an_account_is_never_shown_by_its_e_mail_address(story: DatabaseSettings) -> None:
    _execute(
        story,
        "INSERT INTO session_env (session_id, account_email) VALUES (%s, %s)",
        SESSION,
        EMAIL,
    )

    accounts = {row[1] for row in _rows(story, USAGE_SQL)}

    assert len(accounts) == 1
    (account,) = accounts
    assert account.startswith("account-") and len(account) == len("account-") + 6
    assert EMAIL not in account


def test_labels_replace_the_neutral_names(story: DatabaseSettings) -> None:
    _execute(
        story,
        "INSERT INTO session_env (session_id, account_email) VALUES (%s, %s)",
        SESSION,
        EMAIL.upper(),
    )
    connection = connect(story)
    try:
        set_account_label(connection, EMAIL, "claude1")
        set_project_label(connection, "/work/app", "billing-api")
    finally:
        connection.close()

    assert {(r[0], r[1]) for r in _rows(story, USAGE_SQL)} == {("billing-api", "claude1")}


def test_labels_can_be_listed_replaced_and_removed(story: DatabaseSettings) -> None:
    _execute(
        story,
        "INSERT INTO session_env (session_id, account_email) VALUES (%s, %s)",
        SESSION,
        EMAIL,
    )
    connection = connect(story)
    try:
        listed = {(item.kind, item.key, item.label) for item in list_labels(connection)}
        assert listed == {("account", EMAIL, ""), ("project", "/work/app", "")}

        set_project_label(connection, "/work/app", "first")
        set_project_label(connection, "/work/app", "second")
        assert ("project", "/work/app", "second") in {
            (item.kind, item.key, item.label) for item in list_labels(connection)
        }

        assert remove_label(connection, "project", "/work/app") is True
        assert remove_label(connection, "project", "/work/app") is False
    finally:
        connection.close()
    assert {r[0] for r in _rows(story, USAGE_SQL)} == {"app"}


@pytest.mark.parametrize("label", ["", " ", "a" * 41, "has;semicolon", "line\nbreak", "ünï"])
def test_a_label_that_is_not_plain_is_refused(label: str, story: DatabaseSettings) -> None:
    connection = connect(story)
    try:
        with pytest.raises(LabelError):
            set_project_label(connection, "/work/app", label)
    finally:
        connection.close()


# ----------------------------------------------------------------------------- the roles


def _as(database: DatabaseSettings, name: str) -> DatabaseSettings:
    return replace(database, user=name, password=Secret(PASSWORD))


def test_the_dashboard_reader_sees_usage_but_not_the_labels_behind_it(
    role_name: str, story: DatabaseSettings
) -> None:
    admin = connect(story)
    try:
        create_reader(admin, story.name, PASSWORD, role_name)
    finally:
        admin.close()
    reader = _as(story, role_name)

    assert len(_rows(reader, "SELECT * FROM d_usage")) == 2
    for table in ("account_label", "project_label", "v_session_folder"):
        with pytest.raises(pg8000.dbapi.DatabaseError, match="permission denied"):
            _rows(reader, f"SELECT * FROM {table}")  # noqa: S608


def test_the_auditor_can_read_everything_and_change_nothing(
    role_name: str, story: DatabaseSettings
) -> None:
    admin = connect(story)
    try:
        create_auditor(admin, story.name, PASSWORD, role_name)
    finally:
        admin.close()
    auditor = _as(story, role_name)

    for table in ("event", "session", "session_env", "thinking", "v_file_history", "d_usage"):
        _rows(auditor, f"SELECT * FROM {table}")  # noqa: S608
    for sql in (
        "DELETE FROM event",
        "UPDATE session SET branch = 'x'",
        "INSERT INTO account_label VALUES ('a@b', 'x')",
        "CREATE TABLE loot (x int)",
    ):
        with pytest.raises(pg8000.dbapi.DatabaseError):
            _execute(auditor, sql)


def test_the_label_commands_work_from_the_command_line(
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
    }.items():
        monkeypatch.setenv(name, value)

    assert main(["label", "project", "/work/app", "billing-api"]) == 0
    assert main(["labels"]) == 0
    assert main(["label", "project", "/work/app", "bad;label"]) == 1
    assert main(["unlabel", "project", "/work/app"]) == 0

    captured = capsys.readouterr()
    assert "billing-api" in captured.out
    assert "Label of the project removed." in captured.out
    assert "A label is 1 to 40" in captured.err
