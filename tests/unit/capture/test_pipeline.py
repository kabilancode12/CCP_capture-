import json

from ccp.capture.pipeline import INVALID_RULE, WITHHELD_RULE, prepare_chunk, refused_chunk
from ccp.capture.reader import SourceLine
from ccp.storage.attachments import MIN_EXTERNAL_BYTES, POINTER_KEY

KEY = "sessions/s1/main/000000000000.jsonl"


def _lines(*texts: str) -> list[SourceLine]:
    return [SourceLine(text, len(text.encode()) + 1) for text in texts]


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_clean_lines_pass_through_unchanged() -> None:
    source = _lines('{"type":"user","message":"hello"}', '{"type":"assistant"}')
    chunk = prepare_chunk(KEY, source)
    assert chunk.body == b'{"type":"user","message":"hello"}\n{"type":"assistant"}\n'
    assert chunk.records == [] and chunk.attachments == {}
    assert chunk.source_bytes == sum(line.size for line in source)


def test_secrets_and_personal_data_never_reach_the_body(fakes: dict[str, str]) -> None:
    source = _lines(
        _compact({"type": "user", "text": f"{fakes['email']} {fakes['aws-access-key-id']}"})
    )
    chunk = prepare_chunk(KEY, source)
    text = chunk.body.decode()
    assert fakes["email"] not in text and fakes["aws-access-key-id"] not in text
    assert "[REDACTED:email]" in text


def test_redaction_records_name_the_chunk_line_and_path_but_not_the_value(
    fakes: dict[str, str],
) -> None:
    source = _lines(
        '{"type":"user"}', _compact({"type": "user", "message": {"text": fakes["pan"]}})
    )
    chunk = prepare_chunk(KEY, source)
    assert [(r.rule_id, r.location, r.occurrences) for r in chunk.records] == [
        ("pan", f"{KEY}:2:message.text", 1)
    ]
    assert fakes["pan"] not in repr(chunk.records)


def test_blank_lines_are_skipped_but_their_bytes_still_count() -> None:
    source = _lines('{"a":1}', "", "   ", '{"b":2}')
    chunk = prepare_chunk(KEY, source)
    assert chunk.body == b'{"a":1}\n{"b":2}\n'
    assert chunk.source_bytes == sum(line.size for line in source)


def test_a_chunk_of_only_blank_lines_has_an_empty_body() -> None:
    chunk = prepare_chunk(KEY, _lines("", " "))
    assert chunk.body == b"" and chunk.source_bytes > 0


def test_large_attachments_are_replaced_by_pointers_and_collected() -> None:
    body = {"type": "skill_listing", "text": "x" * (MIN_EXTERNAL_BYTES * 2)}
    line = _compact({"type": "attachment", "uuid": "u1", "sessionId": "s1", "attachment": body})
    chunk = prepare_chunk(KEY, _lines(line))

    entry = json.loads(chunk.body.decode())
    assert POINTER_KEY in entry["attachment"]["text"]
    assert entry["attachment"]["type"] == "skill_listing"  # the kind stays queryable
    assert len(chunk.attachments) == 1
    assert len(chunk.body) < len(line) / 5


def test_attachments_are_cleaned_before_they_are_collected(fakes: dict[str, str]) -> None:
    body = {"type": "skill_listing", "text": f"{fakes['email']} " + "x" * (MIN_EXTERNAL_BYTES * 2)}
    line = _compact({"type": "attachment", "uuid": "u1", "sessionId": "s1", "attachment": body})
    chunk = prepare_chunk(KEY, _lines(line))
    (stored,) = chunk.attachments.values()
    assert fakes["email"].encode() not in stored  # redact first, then store


def test_the_same_attachment_twice_is_collected_once() -> None:
    body = {"type": "skill_listing", "text": "x" * (MIN_EXTERNAL_BYTES * 2)}
    lines = [
        _compact({"type": "attachment", "uuid": u, "sessionId": "s1", "attachment": body})
        for u in ("u1", "u2")
    ]
    assert len(prepare_chunk(KEY, _lines(*lines)).attachments) == 1


def test_a_line_that_is_not_a_json_object_is_replaced_and_reported() -> None:
    chunk = prepare_chunk(KEY, _lines('{"a":1}', "this is not json", "[1,2]"))
    lines = chunk.body.decode().splitlines()
    assert lines[0] == '{"a":1}'
    assert json.loads(lines[1])["type"] == "ccp-invalid-line"
    assert json.loads(lines[2])["type"] == "ccp-invalid-line"
    assert [(r.rule_id, r.location.split(":")[1]) for r in chunk.records] == [
        (INVALID_RULE, "2"),
        (INVALID_RULE, "3"),
    ]
    assert "this is not json" not in chunk.body.decode()


def test_a_secret_in_a_corrupt_line_is_still_redacted_before_it_is_dropped(
    fakes: dict[str, str],
) -> None:
    chunk = prepare_chunk(KEY, _lines(f"garbage {fakes['aws-access-key-id']}"))
    assert fakes["aws-access-key-id"] not in chunk.body.decode()
    assert {r.rule_id for r in chunk.records} == {"aws-access-key-id", INVALID_RULE}


def test_a_line_that_cannot_be_processed_is_withheld(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from ccp.capture import pipeline
    from ccp.redaction import LineResult

    monkeypatch.setattr(pipeline, "redact_line", lambda text, n: LineResult(None, withheld=True))
    chunk = prepare_chunk(KEY, _lines('{"a":"secret-ish"}'))
    assert json.loads(chunk.body.decode())["type"] == "ccp-withheld"
    assert "secret-ish" not in chunk.body.decode()
    assert [r.rule_id for r in chunk.records] == [WITHHELD_RULE]


def test_a_refused_chunk_becomes_one_placeholder_covering_the_same_source_bytes() -> None:
    source = _lines('{"type":"user","message":"private words"}', '{"type":"assistant"}')
    chunk = prepare_chunk(KEY, source)

    placeholder = refused_chunk(source, 413)

    assert placeholder.source_bytes == chunk.source_bytes
    assert json.loads(placeholder.body) == {
        "type": "ccp-withheld",
        "reason": "chunk refused by the API",
        "status": 413,
        "lines": 2,
        "bytes": chunk.source_bytes,
    }
    assert placeholder.body.endswith(b"\n") and b"private words" not in placeholder.body
    assert placeholder.attachments == {} and placeholder.records == []
