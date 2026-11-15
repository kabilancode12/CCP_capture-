"""The tool-result endpoint: large tool outputs stored next to the transcript (no MinIO needed)."""

import hashlib

import pytest
from starlette.testclient import TestClient

from .conftest import AUTH, SESSION, TOKEN, FakeStore

URL = f"/v1/sessions/{SESSION}/tool-results/bs12jynbi.txt"
KEY = f"sessions/{SESSION}/tool-results/bs12jynbi.txt"
BODY = b"line 1\nline 2 [REDACTED:email]\n"


def test_a_file_is_stored_under_its_name(client: TestClient, store: FakeStore) -> None:
    response = client.put(URL, content=BODY, headers=AUTH)
    assert (response.status_code, response.json()) == (201, {"stored": True, "key": KEY})
    assert store.objects == {KEY: BODY}


def test_the_same_file_again_writes_nothing(client: TestClient, store: FakeStore) -> None:
    client.put(URL, content=BODY, headers=AUTH)
    response = client.put(URL, content=BODY, headers=AUTH)
    assert (response.status_code, response.json()) == (200, {"stored": False, "key": KEY})
    assert store.put_calls == 1


def test_a_changed_file_is_stored_next_to_the_first_version_which_is_kept(
    client: TestClient, store: FakeStore
) -> None:
    changed = BODY + b"more\n"
    client.put(URL, content=BODY, headers=AUTH)
    response = client.put(URL, content=changed, headers=AUTH)
    again = client.put(URL, content=changed, headers=AUTH)

    version = f"{KEY}@{hashlib.sha256(changed).hexdigest()[:16]}"
    assert (response.status_code, response.json()) == (200, {"stored": True, "key": version})
    assert (again.status_code, again.json()) == (200, {"stored": False, "key": version})
    assert store.objects == {KEY: BODY, version: changed}  # raw data is never replaced


@pytest.mark.parametrize("name", ["..", "..%2Fx", ".hidden", "a%20b.txt", "chunk.jsonl", "x" * 129])
def test_unsafe_names_are_rejected(name: str, client: TestClient, store: FakeStore) -> None:
    response = client.put(f"/v1/sessions/{SESSION}/tool-results/{name}", content=BODY, headers=AUTH)
    assert response.status_code in (400, 404)  # 404: the router itself refused the path
    assert store.objects == {}


def test_an_unsafe_session_id_is_rejected(client: TestClient) -> None:
    response = client.put("/v1/sessions/bad%20id/tool-results/a.txt", content=BODY, headers=AUTH)
    assert response.status_code == 400


def test_text_that_is_not_utf8_is_rejected(client: TestClient, store: FakeStore) -> None:
    assert client.put(URL, content=b"\xff\xfe", headers=AUTH).status_code == 422
    assert store.objects == {}


def test_a_file_over_the_limit_is_refused(client: TestClient, store: FakeStore) -> None:
    assert client.put(URL, content=b"x" * 2000, headers=AUTH).status_code == 413  # limit: 1024
    assert store.objects == {}


def test_the_token_is_required(client: TestClient, store: FakeStore) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.put(URL, content=BODY, headers=headers)
        assert response.status_code == 401 and TOKEN not in response.text
    assert store.objects == {}


def test_storage_down_gives_503(client: TestClient, store: FakeStore) -> None:
    store.down = True
    assert client.put(URL, content=BODY, headers=AUTH).status_code == 503


def test_a_chunk_cannot_be_uploaded_into_the_tool_results_folder(client: TestClient) -> None:
    url = f"/v1/sessions/{SESSION}/streams/tool-results/chunks/0"
    assert client.put(url, content=b'{"type":"user"}\n', headers=AUTH).status_code == 400
