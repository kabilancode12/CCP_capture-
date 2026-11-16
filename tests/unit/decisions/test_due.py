"""When a pending chat is sent to the model."""

from datetime import UTC, datetime, timedelta

from ccp.decisions.plugin import is_due

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
GAP = timedelta(minutes=5)


def _due(*, quiet: bool, ended: bool, minutes_ago: float | None) -> bool:
    last = None if minutes_ago is None else NOW - timedelta(minutes=minutes_ago)
    return is_due(quiet=quiet, ended=ended, last_done=last, now=NOW, min_gap=GAP)


def test_a_chat_that_is_neither_quiet_nor_ended_is_not_due() -> None:
    assert not _due(quiet=False, ended=False, minutes_ago=None)


def test_a_quiet_chat_that_was_never_processed_is_due() -> None:
    assert _due(quiet=True, ended=False, minutes_ago=None)


def test_a_quiet_chat_processed_a_moment_ago_waits_for_the_gap() -> None:
    assert not _due(quiet=True, ended=False, minutes_ago=1)
    assert _due(quiet=True, ended=False, minutes_ago=5)


def test_a_session_end_is_due_at_once_even_inside_the_gap() -> None:
    assert _due(quiet=False, ended=True, minutes_ago=0.5)
