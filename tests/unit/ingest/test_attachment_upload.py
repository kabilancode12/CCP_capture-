import hashlib
import json

import pytest
from starlette.testclient import TestClient

from ccp.ingest.attachments import AttachmentError, validate_attachment

from .conftest import AUTH, TOKEN, FakeStore

BODY = json.dumps({"type": "skill_listing", "text": "x" * 500}, separators=(",", ":")).encode()
DIGEST = hashlib.sha256(BODY).hexdigest()
URL = f"/v1/attachments/{DIGEST}"
KEY = f"attachments/{DIGEST[:2]}/{DIGEST}"


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------- the validation function


@pytest.mark.parametrize("body", [b'{"a":1}', b'"just a string"', b"42", b"[1,2]", b"null"])
def test_any_json_value_is_accepted_when_its_hash_matches(body: bytes) -> None:
    validate_attachment(_digest(body), body)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "empty"),
        (b"\xff\xfe", "UTF-8"),
        (b"not json", "not valid JSON"),
    ],
)
def test_malformed_bodies_are_rejected(body: bytes, message: str) -> None:
    with pytest.raises(AttachmentError, match=message):
        validate_attachment(_digest(body), body)


def test_a_body_that_does_not_hash_to_its_digest_is_rejected() -> None:
    with pytest.raises(AttachmentError, match="does not match"):
        validate_attachment(_digest(b'{"other":1}'), BODY)


# ---------------------------------------------------------------- the endpoint


def test_an_attachment_is_stored_under_its_digest(client: TestClient, store: FakeStore) -> None:
    response = client.put(URL, content=BODY, headers=AUTH)
    assert response.status_code == 201
    assert response.json() == {"stored": True}
    assert store.objects == {KEY: BODY}


def test_uploading_the_same_attachment_again_writes_nothing(
    client: TestClient, store: FakeStore
) -> None:
    client.put(URL, content=BODY, headers=AUTH)
    again = client.put(URL, content=BODY, headers=AUTH)
    assert (again.status_code, again.json()) == (200, {"stored": False})
    assert store.put_calls == 1


def test_a_body_that_does_not_match_the_url_digest_is_refused_and_not_stored(
    client: TestClient, store: FakeStore
) -> None:
    wrong = f"/v1/attachments/{_digest(b'{}')}"  # the digest of a different body
    assert client.put(wrong, content=BODY, headers=AUTH).status_code == 422
    assert store.objects == {}


@pytest.mark.parametrize("bad", ["abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_a_malformed_digest_is_a_bad_request(client: TestClient, bad: str) -> None:
    assert client.put(f"/v1/attachments/{bad}", content=BODY, headers=AUTH).status_code == 400


def test_invalid_bodies_are_refused(client: TestClient, store: FakeStore) -> None:
    for body in (b"", b"\xff", b"not json"):
        url = f"/v1/attachments/{_digest(body)}"
        assert client.put(url, content=body, headers=AUTH).status_code == 422, body
    assert store.objects == {}


def test_the_token_is_required(client: TestClient, store: FakeStore) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.put(URL, content=BODY, headers=headers)
        assert response.status_code == 401
        assert TOKEN not in response.text
    assert store.objects == {}


def test_an_oversized_attachment_is_refused(client: TestClient, store: FakeStore) -> None:
    big = json.dumps({"t": "x" * 2000}).encode()  # the fixture's limit is 1024 bytes
    assert (
        client.put(f"/v1/attachments/{_digest(big)}", content=big, headers=AUTH).status_code == 413
    )
    assert store.objects == {}


def test_storage_down_gives_503_and_a_retry_works(client: TestClient, store: FakeStore) -> None:
    store.down = True
    response = client.put(URL, content=BODY, headers=AUTH)
    assert response.status_code == 503
    assert "down" not in response.text  # internal details are not leaked

    store.down = False
    assert client.put(URL, content=BODY, headers=AUTH).status_code == 201


def test_only_put_is_allowed(client: TestClient) -> None:
    assert client.get(URL, headers=AUTH).status_code == 405
