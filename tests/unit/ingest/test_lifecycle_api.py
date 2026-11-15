"""The lifecycle endpoint: a session of a chat started or ended (no database needed)."""

from typing import Any

import pytest
from starlette.testclient import TestClient

from ccp.environment import EnvironmentRecord

from .conftest import AUTH, SESSION, TOKEN, FakeRepository

URL = f"/v1/sessions/{SESSION}/lifecycle"
KEY = "6f1c2d3e-0000-4000-8000-00000000abcd"
START = {"kind": "start", "run_key": KEY, "at": "2026-11-15T17:35:02+00:00", "source": "resume",
         "commit": "0123abcd" * 5}  # fmt: skip
END = {"kind": "end", "run_key": KEY, "at": "2026-11-15T18:00:00+00:00", "reason": "logout",
       "commit": None}  # fmt: skip


def test_a_start_is_recorded_with_its_number_in_the_chat(
    client: TestClient, repository: FakeRepository
) -> None:
    response = client.post(URL, json=START, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"recorded": True, "seq": 1}
    ((session_id, event),) = repository.lifecycle
    assert (session_id, event.kind, event.source, event.commit) == (
        SESSION,
        "start",
        "resume",
        START["commit"],
    )
    assert event.at.isoformat() == START["at"]


def test_the_end_of_the_same_session_has_the_same_number(
    client: TestClient, repository: FakeRepository
) -> None:
    client.post(URL, json=START, headers=AUTH)
    response = client.post(URL, json=END, headers=AUTH)
    assert response.json() == {"recorded": True, "seq": 1}
    assert repository.lifecycle[-1][1].reason == "logout"


def test_values_claude_code_may_add_later_are_kept_as_unknown_or_other(
    client: TestClient, repository: FakeRepository
) -> None:
    client.post(URL, json={**START, "source": "something-new", "extra": 1}, headers=AUTH)
    client.post(URL, json={**END, "reason": "bypass_permissions_disabled"}, headers=AUTH)
    assert [e.source or e.reason for _, e in repository.lifecycle] == ["unknown", "other"]


def test_a_time_without_a_zone_is_read_as_utc(
    client: TestClient, repository: FakeRepository
) -> None:
    client.post(URL, json={**START, "at": "2026-11-15T17:35:02"}, headers=AUTH)
    assert repository.lifecycle[0][1].at.utcoffset() is not None


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "pause", "run_key": KEY, "at": START["at"]},
        {"kind": "start", "run_key": "short", "at": START["at"]},
        {"kind": "start", "run_key": "../../etc/passwd", "at": START["at"]},
        {"kind": "start", "run_key": KEY, "at": "yesterday"},
        {"kind": "start", "run_key": KEY, "at": 5},
        {"kind": "start", "run_key": KEY, "at": START["at"], "commit": "not-a-sha"},
        {"something": "else"},
        [1, 2],
    ],
)
def test_malformed_events_are_rejected(
    body: Any, client: TestClient, repository: FakeRepository
) -> None:
    assert client.post(URL, json=body, headers=AUTH).status_code == 422
    assert repository.lifecycle == []


def test_a_body_that_is_not_json_is_rejected(client: TestClient) -> None:
    assert client.post(URL, content=b"\xff\xfe", headers=AUTH).status_code == 422


def test_a_large_body_is_refused(client: TestClient, repository: FakeRepository) -> None:
    body = {**START, "padding": "x" * 5000}
    assert client.post(URL, json=body, headers=AUTH).status_code == 413
    assert repository.lifecycle == []


def test_the_token_is_required(client: TestClient, repository: FakeRepository) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.post(URL, json=START, headers=headers)
        assert response.status_code == 401
        assert TOKEN not in response.text
    assert repository.lifecycle == []


def test_an_unsafe_session_id_is_rejected(client: TestClient) -> None:
    assert (
        client.post("/v1/sessions/bad%20id/lifecycle", json=START, headers=AUTH).status_code == 400
    )


def test_a_run_key_of_another_chat_is_a_conflict(
    client: TestClient, repository: FakeRepository
) -> None:
    client.post(URL, json=START, headers=AUTH)
    other = client.post("/v1/sessions/other-chat/lifecycle", json=START, headers=AUTH)
    assert other.status_code == 409


def test_database_down_gives_503(client: TestClient, repository: FakeRepository) -> None:
    repository.down = True
    response = client.post(URL, json=START, headers=AUTH)
    assert response.status_code == 503 and "down" not in response.text


def test_only_post_is_allowed(client: TestClient) -> None:
    assert client.put(URL, json=START, headers=AUTH).status_code == 405


def test_the_new_environment_fields_are_accepted(
    client: TestClient, repository: FakeRepository
) -> None:
    fields = {
        "account_uuid": "u-1",
        "full_name": "Dev Eloper",
        "organisation_role": "admin",
        "machine_key": "0123456789abcdef",
        "folder": "/work/app",
        "git_remote": "https://github.com/acme/app.git",
        "git_user_name": "Dev",
        "git_user_email": "dev@example.com",
    }
    response = client.put(f"/v1/sessions/{SESSION}/environment", json=fields, headers=AUTH)
    assert response.status_code == 200
    assert repository.environments == [(SESSION, EnvironmentRecord(**fields))]
