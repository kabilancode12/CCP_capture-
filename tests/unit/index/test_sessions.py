"""Inferring the sessions of a chat from time stamps."""

from datetime import UTC, datetime, timedelta

from ccp.index.sessions import (
    DEFAULT_IDLE_MINUTES,
    idle_minutes_from_environment,
    infer_sessions,
)

START = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def test_no_time_stamps_give_no_sessions() -> None:
    assert infer_sessions([]) == []


def test_lines_close_together_are_one_session() -> None:
    [only] = infer_sessions([_at(0), _at(10), _at(35)])  # each gap is 30 minutes or less
    assert (only.seq, only.started_at, only.ended_at) == (1, _at(0), _at(35))


def test_a_gap_longer_than_the_limit_starts_a_new_session() -> None:
    first, second, third = infer_sessions([_at(0), _at(5), _at(36), _at(40), _at(200)])
    assert (first.started_at, first.ended_at) == (_at(0), _at(5))
    assert (second.seq, second.started_at, second.ended_at) == (2, _at(36), _at(40))
    assert (third.seq, third.started_at, third.ended_at) == (3, _at(200), _at(200))


def test_exactly_the_limit_does_not_split() -> None:
    assert len(infer_sessions([_at(0), _at(30)])) == 1
    assert len(infer_sessions([_at(0), _at(30.1)])) == 2


def test_the_limit_can_be_changed() -> None:
    assert len(infer_sessions([_at(0), _at(11)], idle_minutes=10)) == 2


def test_a_time_that_goes_backwards_never_splits_or_shortens_a_session() -> None:
    [only] = infer_sessions([_at(20), _at(5), _at(25)])
    assert (only.started_at, only.ended_at) == (_at(20), _at(25))


def test_the_idle_limit_is_read_from_the_environment() -> None:
    assert idle_minutes_from_environment({}) == DEFAULT_IDLE_MINUTES
    assert idle_minutes_from_environment({"CCP_SESSION_IDLE_MINUTES": "45"}) == 45
    assert idle_minutes_from_environment({"CCP_SESSION_IDLE_MINUTES": "0"}) == DEFAULT_IDLE_MINUTES
    assert idle_minutes_from_environment({"CCP_SESSION_IDLE_MINUTES": "x"}) == DEFAULT_IDLE_MINUTES
    assert idle_minutes_from_environment({"CCP_SESSION_IDLE_MINUTES": "9999"}) == 30
