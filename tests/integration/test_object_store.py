"""Object store tests against a real MinIO (see conftest.py)."""

import json
from dataclasses import replace

import pytest

from ccp.settings import StorageSettings
from ccp.storage import ObjectNotFoundError, ObjectStore, StorageError, connect_store
from ccp.storage.attachments import MIN_EXTERNAL_BYTES, externalise_line, restore_line
from ccp.storage.keys import attachment_key, session_prefix, transcript_chunk_key

pytestmark = pytest.mark.integration


def test_ensure_bucket_can_be_called_repeatedly(object_store: ObjectStore) -> None:
    object_store.ensure_bucket()
    object_store.ensure_bucket()  # no error the second time


def test_an_object_can_be_stored_and_read_back(object_store: ObjectStore) -> None:
    object_store.put("a/b.txt", b"hello \xe0\xa4\xa8")  # includes non-ASCII bytes
    assert object_store.get("a/b.txt") == b"hello \xe0\xa4\xa8"


def test_an_empty_object_is_allowed(object_store: ObjectStore) -> None:
    object_store.put("empty", b"")
    assert object_store.get("empty") == b""


def test_putting_the_same_key_again_replaces_the_object(object_store: ObjectStore) -> None:
    object_store.put("k", b"one")
    object_store.put("k", b"two")
    assert object_store.get("k") == b"two"


def test_reading_a_missing_object_raises_a_clear_error(object_store: ObjectStore) -> None:
    with pytest.raises(ObjectNotFoundError):
        object_store.get("does/not/exist")


def test_exists(object_store: ObjectStore) -> None:
    assert object_store.exists("k") is False
    object_store.put("k", b"x")
    assert object_store.exists("k") is True


def test_put_if_absent_never_overwrites(object_store: ObjectStore) -> None:
    assert object_store.put_if_absent("k", b"first") is True
    assert object_store.put_if_absent("k", b"second") is False
    assert object_store.get("k") == b"first"


def test_list_keys_by_prefix_in_alphabetical_order(object_store: ObjectStore) -> None:
    for key in ("s/2/b", "s/1/z", "s/1/a", "other/x"):
        object_store.put(key, b"x")
    assert object_store.list_keys("s/") == ["s/1/a", "s/1/z", "s/2/b"]
    assert object_store.list_keys("nothing/") == []


def test_delete_removes_the_object_and_ignores_missing_keys(object_store: ObjectStore) -> None:
    object_store.put("k", b"x")
    object_store.delete("k")
    assert object_store.exists("k") is False
    object_store.delete("k")  # deleting again is fine


def test_unreachable_storage_raises_storage_error(storage_settings: StorageSettings) -> None:
    nowhere = replace(storage_settings, endpoint="127.0.0.1:1")  # nothing listens on port 1
    store = connect_store(nowhere, create_bucket=False)
    with pytest.raises(StorageError, match="unreachable"):
        store.put("k", b"x")


def test_wrong_credentials_raise_storage_error(storage_settings: StorageSettings) -> None:
    from ccp.settings import Secret

    wrong = replace(storage_settings, secret_key=Secret("definitely-wrong-secret"))
    with pytest.raises(StorageError):
        connect_store(wrong)


# ---------------------------------------------------------------- keys and attachments, for real


def test_transcript_chunks_of_a_session_list_in_offset_order(object_store: ObjectStore) -> None:
    for offset in (4096, 0, 130_000):
        object_store.put(transcript_chunk_key("sess-1", "main", offset), b"{}\n")
    object_store.put(transcript_chunk_key("sess-2", "main", 0), b"{}\n")

    keys = object_store.list_keys(session_prefix("sess-1"))
    assert [k.rsplit("/", 1)[1] for k in keys] == [
        "000000000000.jsonl",
        "000000004096.jsonl",
        "000000130000.jsonl",
    ]


def test_re_uploading_a_chunk_uses_the_same_key(object_store: ObjectStore) -> None:
    key = transcript_chunk_key("sess-1", "main", 512)
    object_store.put(key, b"first\n")
    object_store.put(key, b"first\n")  # a retry of the same chunk
    assert object_store.list_keys(session_prefix("sess-1")) == [key]


def test_attachment_deduplication_end_to_end(object_store: ObjectStore) -> None:
    body = {"type": "skill_listing", "text": "x" * (MIN_EXTERNAL_BYTES * 3)}

    def line(uuid: str, session: str) -> str:
        entry = {"type": "attachment", "uuid": uuid, "sessionId": session, "attachment": body}
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))

    first = externalise_line(line("a", "s1"), object_store)
    second = externalise_line(line("b", "s2"), object_store)

    assert (first.new_objects, second.new_objects, second.reused_objects) == (1, 0, 1)
    assert len(object_store.list_keys("attachments/")) == 1  # one stored object, two lines
    assert restore_line(first.text, object_store) == line("a", "s1")
    assert restore_line(second.text, object_store) == line("b", "s2")
    assert object_store.exists(attachment_key(json.loads(first.text)["attachment"]["$ccp_ref"]))
