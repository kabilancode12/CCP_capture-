"""The six saved questions (views from migration 011), answered on one invented session.

The session is indexed by the real indexer, so these tests cover the whole path from transcript
lines to answers. The story: the developer asks for a fix; the AI edits, the tests fail, it edits
again, the tests pass; a command is refused; the conversation compacts; the developer rewinds
and asks something else, which launches a sub-agent. Every expected value below can be read off
that story.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from tests.integration.story import (
    SESSION,
    index_story,
    make_call,
    make_edit,
    make_failed,
    make_prompt,
    make_result,
)

from ccp.db.connection import connect
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore

pytestmark = pytest.mark.integration


def _time(minute: int) -> datetime:
    return datetime(2026, 7, 1, 10, minute, tzinfo=UTC)


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


def test_question_1_cost_and_models_per_session(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT session_id, total_cost_usd::text, has_unknown_model_cost, models, input_tokens, "
        "output_tokens, branch FROM v_session_cost",
    )

    assert rows == [(SESSION, "1.500000", False, ["model-a", "helper-model"], 1000, 60, None)]


def test_question_2_every_change_to_a_file_with_its_prompt(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT occurred_at, tool, change_type, actor_class, lines_added, lines_removed, "
        "is_abandoned, prompt FROM v_file_history WHERE path = 'src/a.py' ORDER BY occurred_at",
    )

    assert rows == [
        (_time(2), "Edit", "modified", "ai_edit", 2, 1, False, "Fix the bug in a.py"),
        (_time(6), "Edit", "modified", "ai_edit", 2, 1, False, "Fix the bug in a.py"),
    ]
    assert _rows(story, "SELECT count(*) FROM v_file_history WHERE path = 'other.py'") == [(0,)]


def test_question_3_sub_agents_with_their_instructions_and_result(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT agent_type, description, spawn_depth, instructions, result, event_count, "
        "parent_agent_id FROM v_agent_run",
    )

    assert rows == [("Explore", None, 1, "Explore the code", "Found it.", 2, None)]


def test_question_4_human_decisions_denials_and_rewinds(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT decision_type, occurred_at, detail, tool FROM v_human_decision ORDER BY decision_type",
    )

    assert rows == [
        ("denial", _time(10), "user-rejected", "Bash"),
        ("rewind", _time(14), "abandoned prompt", None),  # when the replacing prompt was typed
    ]


def test_question_5_where_a_session_compacted(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT occurred_at, trigger, pre_tokens, post_tokens, prompts_before, summary "
        "FROM v_compaction",
    )

    assert rows == [(_time(11), "auto", 9000, 1200, 1, "SUMMARY")]


def test_question_6_tests_that_failed_and_then_passed_after_edits(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT framework, failed_command, failed_at, passed_at, edits_between "
        "FROM v_test_fix_cycle",
    )

    assert rows == [("pytest", "pytest -q", _time(3), _time(7), 1)]


def test_a_plan_decision_is_listed_as_approved_or_rejected(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    plan = [
        make_prompt("p1", None, 0, "Plan it"),
        make_call("a1", "p1", 1, "ExitPlanMode", "pl1", plan="the plan"),
        make_result("r1", "a1", 2, "pl1"),
        make_call("a2", "r1", 3, "ExitPlanMode", "pl2", plan="another plan"),
        make_failed("r2", "a2", 4, "pl2", "The user rejected the plan"),
    ]
    index_story(database, object_store, plan)

    rows = _rows(
        database,
        "SELECT decision_type, detail, tool FROM v_human_decision ORDER BY occurred_at",
    )

    assert rows == [
        ("plan_decision", "approved", "ExitPlanMode"),
        ("plan_decision", "rejected", "ExitPlanMode"),
    ]


def test_a_failure_that_is_never_followed_by_a_pass_is_not_a_fix_cycle(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    story_without_pass = [
        make_prompt("p1", None, 0, "Run the tests"),
        make_call("a1", "p1", 1, "Bash", "t1", command="pytest"),
        make_failed("r1", "a1", 2, "t1", "Exit code 1"),
    ]
    index_story(database, object_store, story_without_pass)

    assert _rows(database, "SELECT count(*) FROM v_test_fix_cycle") == [(0,)]
    assert _rows(database, "SELECT count(*) FROM test_run WHERE passed = FALSE") == [(1,)]


def test_work_on_an_abandoned_branch_is_kept_and_flagged(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    rewound = [
        make_prompt("p0", None, 0, "Start"),
        make_call("a0", "p0", 1, "Bash", "x0", command="ls"),
        make_prompt("p1", "a0", 2, "First idea"),
        make_call("a1", "p1", 3, "Edit", "e1"),
        make_edit("r1", "a1", 4, "e1"),
        make_prompt("p2", "a0", 6, "Second idea"),
    ]
    index_story(database, object_store, rewound)

    rows = _rows(database, "SELECT is_abandoned, prompt FROM v_file_history")

    assert rows == [(True, "First idea")]
    assert _rows(database, "SELECT count(*) FROM v_file_history WHERE NOT is_abandoned") == [(0,)]


def test_the_views_are_empty_but_work_on_an_empty_database(database: DatabaseSettings) -> None:
    for view in (
        "v_session_cost",
        "v_file_history",
        "v_agent_run",
        "v_human_decision",
        "v_compaction",
        "v_test_fix_cycle",
    ):
        assert _rows(database, f"SELECT count(*) FROM {view}") == [(0,)]  # noqa: S608
