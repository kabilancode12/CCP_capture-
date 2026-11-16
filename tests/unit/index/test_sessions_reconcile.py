from datetime import UTC, datetime, timedelta

from ccp.index.sessions import ClientSession, reconcile_client_sessions

T = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return T + timedelta(minutes=minutes)


def test_guessed_end_moves_to_the_last_message_and_a_late_burst_becomes_its_own_session() -> None:
    clients = [
        ClientSession(1, _at(0), _at(20), "other"),  # a real end stays
        ClientSession(2, _at(40), _at(40), "idle_timeout"),
        ClientSession(3, _at(100), _at(100), "idle_timeout"),
    ]
    times = [_at(1), _at(19), _at(41), _at(45), _at(300), _at(310)]
    ends, tails = reconcile_client_sessions(clients, times, 30)
    assert ends == {2: _at(45), 3: _at(100)}  # nothing happened in the third opening
    assert tails == [(_at(300), _at(310))]  # the burst long after it has no reported start


def test_no_messages_means_the_session_ends_where_it_started() -> None:
    clients = [ClientSession(9, _at(5), _at(500), "idle_timeout")]
    assert reconcile_client_sessions(clients, [], 30) == ({9: _at(5)}, [])


def test_a_session_that_is_still_open_keeps_no_end() -> None:
    clients = [ClientSession(9, _at(5), None, None)]
    assert reconcile_client_sessions(clients, [_at(6)], 30) == ({}, [])
