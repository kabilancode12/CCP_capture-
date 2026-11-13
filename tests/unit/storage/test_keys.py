import hashlib

import pytest

from ccp.storage.keys import (
    MAIN_STREAM,
    attachment_key,
    session_prefix,
    stream_of,
    transcript_chunk_key,
)

SESSION = "4a64fad3-ca80-49bc-a42e-87f42b332b4b"


def test_chunk_key_layout() -> None:
    assert (
        transcript_chunk_key(SESSION, MAIN_STREAM, 0)
        == f"sessions/{SESSION}/main/000000000000.jsonl"
    )
    assert transcript_chunk_key(SESSION, "agent-a7c5c9", 4096).endswith(
        "/agent-a7c5c9/000000004096.jsonl"
    )


def test_the_same_chunk_always_gets_the_same_key() -> None:
    assert transcript_chunk_key(SESSION, "main", 10) == transcript_chunk_key(SESSION, "main", 10)


def test_chunk_keys_sort_in_offset_order() -> None:
    keys = [transcript_chunk_key(SESSION, "main", offset) for offset in (900, 5, 100_000, 70)]
    assert sorted(keys) == [transcript_chunk_key(SESSION, "main", o) for o in (5, 70, 900, 100_000)]


@pytest.mark.parametrize(
    "bad",
    ["", "../other", "a/b", "a\\b", "..", ".hidden", "-lead", "x" * 129, "with space", "a..b"],
)
def test_unsafe_ids_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid session id"):
        transcript_chunk_key(bad, "main", 0)
    with pytest.raises(ValueError, match="Invalid stream"):
        transcript_chunk_key(SESSION, bad, 0)


def test_negative_offset_is_rejected() -> None:
    with pytest.raises(ValueError, match="offset"):
        transcript_chunk_key(SESSION, "main", -1)


def test_session_prefix_matches_its_chunks() -> None:
    assert transcript_chunk_key(SESSION, "main", 0).startswith(session_prefix(SESSION))
    with pytest.raises(ValueError):
        session_prefix("../x")


def test_attachment_key_fans_out_by_the_first_two_digits() -> None:
    digest = hashlib.sha256(b"body").hexdigest()
    assert attachment_key(digest) == f"attachments/{digest[:2]}/{digest}"


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_invalid_digests_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="Invalid digest"):
        attachment_key(bad)


@pytest.mark.parametrize("stream", ["main", "agent-a1", "agent-a1.meta"])
def test_the_stream_of_a_chunk_key_is_found(stream: str) -> None:
    assert stream_of(transcript_chunk_key(SESSION, stream, 42)) == stream


@pytest.mark.parametrize(
    "key", ["", "sessions/s1/main", "attachments/ab/abcd", "sessions/a/b/c/d.jsonl"]
)
def test_something_that_is_not_a_chunk_key_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="chunk key"):
        stream_of(key)
