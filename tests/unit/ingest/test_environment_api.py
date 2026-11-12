from starlette.testclient import TestClient

from ccp.environment import EnvironmentRecord

from .conftest import AUTH, SESSION, TOKEN, FakeRepository

URL = f"/v1/sessions/{SESSION}/environment"
GOOD = {"surface": "terminal", "os_name": "Windows", "account_email": "dev@example.com"}


def test_a_record_is_stored(client: TestClient, repository: FakeRepository) -> None:
    response = client.put(URL, json=GOOD, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"recorded": 3}
    assert repository.environments == [(SESSION, EnvironmentRecord(**GOOD))]


def test_partial_records_are_passed_on_as_they_are(
    client: TestClient, repository: FakeRepository
) -> None:
    client.put(URL, json={"surface": "terminal"}, headers=AUTH)
    client.put(URL, json={"claude_code_version": "2.1.289"}, headers=AUTH)
    assert [r.to_payload() for _, r in repository.environments] == [
        {"surface": "terminal"},
        {"claude_code_version": "2.1.289"},
    ]  # merging is the repository's job (tested against the real database)


def test_the_token_is_required(client: TestClient, repository: FakeRepository) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.put(URL, json=GOOD, headers=headers)
        assert response.status_code == 401
        assert TOKEN not in response.text
    assert repository.environments == []


def test_malformed_payloads_are_rejected(client: TestClient, repository: FakeRepository) -> None:
    assert client.put(URL, content=b"not json", headers=AUTH).status_code == 422
    assert client.put(URL, content=b"\xff", headers=AUTH).status_code == 422
    assert client.put(URL, json={}, headers=AUTH).status_code == 422
    assert client.put(URL, json={"surface": 5}, headers=AUTH).status_code == 422
    assert client.put(URL, json={"unknown_field": "x"}, headers=AUTH).status_code == 422
    assert client.put(URL, json={"surface": "a\nb"}, headers=AUTH).status_code == 422
    assert repository.environments == []


def test_an_unsafe_session_id_is_rejected(client: TestClient) -> None:
    assert (
        client.put("/v1/sessions/bad%20id/environment", json=GOOD, headers=AUTH).status_code == 400
    )


def test_database_down_gives_503_and_a_retry_works(
    client: TestClient, repository: FakeRepository
) -> None:
    repository.down = True
    response = client.put(URL, json=GOOD, headers=AUTH)
    assert response.status_code == 503
    assert "down" not in response.text

    repository.down = False
    assert client.put(URL, json=GOOD, headers=AUTH).status_code == 200


def test_only_put_is_allowed(client: TestClient) -> None:
    assert client.post(URL, json=GOOD, headers=AUTH).status_code == 405
