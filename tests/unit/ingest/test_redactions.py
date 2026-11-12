import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from ccp.ingest.redactions import MAX_RECORDS, RedactionReportError, parse_report
from ccp.redaction import Hit
from ccp.redaction.records import RedactionRecord, records_from_hits

from .conftest import AUTH, SESSION, TOKEN, FakeRepository

URL = f"/v1/sessions/{SESSION}/redactions"
KEY = "sessions/s1/main/000000000000.jsonl"
GOOD = {
    "records": [{"rule_id": "aadhaar", "location": f"{KEY}:3:message.content", "occurrences": 2}]
}


# ---------------------------------------------------------------- building records (client side)


def test_hits_become_records_with_the_chunk_key_in_the_location() -> None:
    records = records_from_hits([Hit("email", 2, "message.text")], KEY)
    assert records == [RedactionRecord("email", f"{KEY}:2:message.text", 1)]


def test_repeated_hits_at_one_place_are_counted_not_duplicated() -> None:
    hits = [Hit("email", 1, "a"), Hit("email", 1, "a"), Hit("email", 1, "b"), Hit("pan", 1, "a")]
    records = records_from_hits(hits, KEY)
    assert {(r.rule_id, r.location.rsplit(":", 1)[1]): r.occurrences for r in records} == {
        ("email", "a"): 2,
        ("email", "b"): 1,
        ("pan", "a"): 1,
    }


def test_no_hits_give_no_records() -> None:
    assert records_from_hits([], KEY) == []


# ---------------------------------------------------------------- validating a report (server side)


def test_a_good_report_is_parsed() -> None:
    assert parse_report(GOOD) == [RedactionRecord("aadhaar", f"{KEY}:3:message.content", 2)]


def test_occurrences_default_to_one() -> None:
    record = {"rule_id": "jwt", "location": "x"}
    assert parse_report({"records": [record]})[0].occurrences == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"records": "no"},
        {"records": [1]},
        {"records": [{"rule_id": "Bad Rule", "location": "x"}]},
        {"records": [{"rule_id": "", "location": "x"}]},
        {"records": [{"rule_id": "ok", "location": ""}]},
        {"records": [{"rule_id": "ok", "location": "x" * 513}]},
        {"records": [{"rule_id": "ok", "location": "x", "occurrences": 0}]},
        {"records": [{"rule_id": "ok", "location": "x", "occurrences": True}]},
        {"records": [{"rule_id": "ok", "location": "x", "occurrences": "2"}]},
        {"records": [{"rule_id": "ok", "location": 5}]},
    ],
)
def test_malformed_reports_are_rejected(payload: Any) -> None:
    with pytest.raises(RedactionReportError):
        parse_report(payload)


def test_too_many_records_are_rejected() -> None:
    many = {"records": [{"rule_id": "ok", "location": "x"}] * (MAX_RECORDS + 1)}
    with pytest.raises(RedactionReportError, match="too many"):
        parse_report(many)


# ---------------------------------------------------------------- the endpoint


def test_a_report_is_recorded(client: TestClient, repository: FakeRepository) -> None:
    response = client.post(URL, json=GOOD, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"recorded": 1}
    assert repository.redactions == [
        (SESSION, [RedactionRecord("aadhaar", f"{KEY}:3:message.content", 2)])
    ]


def test_an_empty_report_is_fine(client: TestClient, repository: FakeRepository) -> None:
    assert client.post(URL, json={"records": []}, headers=AUTH).json() == {"recorded": 0}


def test_the_token_is_required(client: TestClient, repository: FakeRepository) -> None:
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.post(URL, json=GOOD, headers=headers)
        assert response.status_code == 401
        assert TOKEN not in response.text
    assert repository.redactions == []


def test_bad_json_and_bad_shape_are_rejected(
    client: TestClient, repository: FakeRepository
) -> None:
    assert client.post(URL, content=b"not json", headers=AUTH).status_code == 422
    assert client.post(URL, content=b"\xff", headers=AUTH).status_code == 422
    assert client.post(URL, json={"records": [{"rule_id": "BAD"}]}, headers=AUTH).status_code == 422
    assert repository.redactions == []


def test_an_unsafe_session_id_is_rejected(client: TestClient) -> None:
    url = "/v1/sessions/bad%20id/redactions"
    assert client.post(url, json=GOOD, headers=AUTH).status_code == 400


def test_an_oversized_report_is_rejected(client: TestClient) -> None:
    big = json.dumps({"records": [{"rule_id": "ok", "location": "x" * 400}] * 10})
    assert client.post(URL, content=big, headers=AUTH).status_code == 413  # limit is 1024 bytes


def test_database_down_gives_503_and_a_retry_works(
    client: TestClient, repository: FakeRepository
) -> None:
    repository.down = True
    response = client.post(URL, json=GOOD, headers=AUTH)
    assert response.status_code == 503
    assert "down" not in response.text

    repository.down = False
    assert client.post(URL, json=GOOD, headers=AUTH).status_code == 200


def test_only_post_is_allowed(client: TestClient) -> None:
    assert client.get(URL, headers=AUTH).status_code == 405
