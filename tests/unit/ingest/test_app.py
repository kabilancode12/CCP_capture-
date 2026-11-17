from starlette.testclient import TestClient

from .conftest import AUTH, LINE, SESSION, TOKEN, URL, FakeRepository, FakeStore

KEY = f"sessions/{SESSION}/main/000000000000.jsonl"


# ---------------------------------------------------------------- health


def test_health_is_ok_without_a_token(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "storage": "ok"}


def test_health_reports_which_part_is_down(
    client: TestClient, store: FakeStore, repository: FakeRepository
) -> None:
    repository.down = True
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unavailable", "storage": "ok"}

    repository.down, store.down = False, True
    assert client.get("/health").json()["storage"] == "unavailable"


# ---------------------------------------------------------------- authentication


def test_requests_without_a_valid_token_are_refused(client: TestClient, store: FakeStore) -> None:
    for headers in (
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},  # right value, wrong scheme
        {"Authorization": "Bearer"},
        {"Authorization": f"Bearer {TOKEN}x"},
    ):
        response = client.put(URL, content=LINE, headers=headers)
        assert response.status_code == 401, headers
        assert response.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in response.text
    assert store.objects == {}  # nothing was stored


def test_the_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    response = client.put(URL, content=LINE, headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 201


# ---------------------------------------------------------------- storing chunks


def test_a_new_chunk_is_stored_and_recorded(
    client: TestClient, store: FakeStore, repository: FakeRepository
) -> None:
    response = client.put(URL, content=LINE, headers=AUTH)

    assert response.status_code == 201
    assert response.json() == {"stored": True}
    assert store.objects == {KEY: LINE}
    assert repository.recorded == [SESSION]


def test_the_offset_in_the_url_names_the_stored_chunk(client: TestClient, store: FakeStore) -> None:
    url = f"/v1/sessions/{SESSION}/streams/main/chunks/5000"
    assert client.put(url, content=LINE, headers=AUTH).status_code == 201
    assert list(store.objects) == [f"sessions/{SESSION}/main/000000005000.jsonl"]


def test_a_sub_agent_chunk_goes_under_its_own_stream(client: TestClient, store: FakeStore) -> None:
    url = f"/v1/sessions/{SESSION}/streams/agent-a7c5c9/chunks/0"
    assert client.put(url, content=LINE, headers=AUTH).status_code == 201
    assert list(store.objects) == [f"sessions/{SESSION}/agent-a7c5c9/000000000000.jsonl"]


def test_sending_the_same_chunk_twice_stores_one_copy(
    client: TestClient, store: FakeStore, repository: FakeRepository
) -> None:
    first = client.put(URL, content=LINE, headers=AUTH)
    second = client.put(URL, content=LINE, headers=AUTH)  # a retry

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == {"stored": False}
    assert store.put_calls == 1  # written once
    assert len(store.objects) == 1
    # The database step is repeated on purpose, so a crash between the two steps heals itself.
    assert repository.recorded == [SESSION, SESSION]


def test_a_different_chunk_at_an_existing_offset_is_a_conflict(
    client: TestClient, store: FakeStore
) -> None:
    client.put(URL, content=LINE, headers=AUTH)
    response = client.put(URL, content=b'{"type":"other"}\n', headers=AUTH)

    assert response.status_code == 409
    assert store.objects[KEY] == LINE  # the original is untouched


# ---------------------------------------------------------------- bad input


def test_unsafe_ids_are_rejected(client: TestClient, store: FakeStore) -> None:
    for session, stream in (
        ("bad%20id", "main"),
        ("%2e%2e", "main"),  # encoded "..": a client would normalise a literal one away
        (SESSION, "a..b"),
        (SESSION, "-x"),
    ):
        url = f"/v1/sessions/{session}/streams/{stream}/chunks/0"
        assert client.put(url, content=LINE, headers=AUTH).status_code == 400, url
    assert store.objects == {}


def test_a_non_numeric_offset_is_not_found(client: TestClient) -> None:
    url = f"/v1/sessions/{SESSION}/streams/main/chunks/abc"
    assert client.put(url, content=LINE, headers=AUTH).status_code == 404


def test_malformed_chunks_are_rejected_and_not_stored(client: TestClient, store: FakeStore) -> None:
    for body in (b"", b'{"a":1}', b"not json\n", b"[1]\n", b"\xff\n"):
        assert client.put(URL, content=body, headers=AUTH).status_code == 422, body
    assert store.objects == {}


def test_an_oversized_chunk_is_rejected(client: TestClient, store: FakeStore) -> None:
    big = b'{"a":"' + b"x" * 2000 + b'"}\n'  # the fixture's limit is 1024 bytes
    assert client.put(URL, content=big, headers=AUTH).status_code == 413
    assert store.objects == {}


def test_unknown_routes_and_methods(client: TestClient) -> None:
    assert client.get("/nope").status_code == 404
    assert client.get(URL, headers=AUTH).status_code == 405  # chunks are written with PUT only


# ---------------------------------------------------------------- failures of dependencies


def test_storage_down_gives_503_and_a_retry_works(
    client: TestClient, store: FakeStore, repository: FakeRepository
) -> None:
    store.down = True
    response = client.put(URL, content=LINE, headers=AUTH)
    assert response.status_code == 503
    assert "down" not in response.text  # internal details are not leaked
    assert repository.recorded == []

    store.down = False  # the client retries later
    assert client.put(URL, content=LINE, headers=AUTH).status_code == 201


def test_database_down_after_storing_heals_on_retry(
    client: TestClient, store: FakeStore, repository: FakeRepository
) -> None:
    repository.down = True
    assert client.put(URL, content=LINE, headers=AUTH).status_code == 503
    assert KEY in store.objects  # the chunk was stored, but the database step failed

    repository.down = False
    retry = client.put(URL, content=LINE, headers=AUTH)
    assert retry.status_code == 200  # identical chunk already stored...
    assert repository.recorded == [SESSION]  # ...and now the database step has happened
