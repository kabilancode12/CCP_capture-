import json
import re

from ccp.redaction import Rule, redact_line
from ccp.redaction.redactor import redact_value


def _line(**fields: object) -> str:
    return json.dumps(fields)


def test_clean_line_is_returned_byte_for_byte() -> None:
    raw = '{"type": "user",   "message": "nothing sensitive",\t"n": 1}'  # odd spacing kept
    result = redact_line(raw, 7)
    assert result.text == raw
    assert result.hits == []
    assert not result.withheld


def test_hit_records_rule_line_and_path_but_never_the_value(fakes: dict[str, str]) -> None:
    raw = _line(message={"content": [{"type": "text", "text": f"mail {fakes['email']}"}]})
    result = redact_line(raw, 42)

    assert [(h.rule_id, h.line, h.path) for h in result.hits] == [
        ("email", 42, "message.content[0].text")
    ]
    assert fakes["email"] not in repr(result.hits)
    assert result.text is not None
    assert fakes["email"] not in result.text


def test_redacted_line_is_still_valid_json(fakes: dict[str, str]) -> None:
    raw = _line(a={"b": [fakes["aws-access-key-id"], "ok", 5, None, True]})
    result = redact_line(raw, 1)
    assert result.text is not None
    parsed = json.loads(result.text)
    assert parsed["a"]["b"] == ["[REDACTED:aws-access-key-id]", "ok", 5, None, True]


def test_secrets_in_dictionary_keys_are_redacted(fakes: dict[str, str]) -> None:
    result = redact_line(_line(**{fakes["github-token"]: "value"}), 1)
    assert result.text is not None
    assert fakes["github-token"] not in result.text
    assert [h.rule_id for h in result.hits] == ["github-token"]


def test_a_secret_key_never_appears_in_the_hit_location(fakes: dict[str, str]) -> None:
    # A real bug: the location was built from the original key, so a key that is itself a secret
    # would have been copied into the redaction log.
    raw = _line(**{fakes["github-token"]: {"inner": fakes["email"]}})
    result = redact_line(raw, 1)

    assert fakes["github-token"] not in repr(result.hits)
    assert fakes["email"] not in repr(result.hits)
    assert sorted((h.rule_id, h.path) for h in result.hits) == [
        ("email", "[REDACTED:github-token].inner"),
        ("github-token", "[REDACTED:github-token]"),
    ]


def test_multiple_hits_in_one_line_are_all_reported(fakes: dict[str, str]) -> None:
    raw = _line(a=fakes["pan"], b=f"call {fakes['indian-mobile']}", c=fakes["jwt"])
    result = redact_line(raw, 3)
    assert sorted(h.rule_id for h in result.hits) == ["indian-mobile", "jwt", "pan"]


def test_non_json_line_is_cleaned_as_plain_text(fakes: dict[str, str]) -> None:
    result = redact_line(f"partial line {fakes['email']} {{", 9)  # a cut-off last line
    assert result.text == "partial line [REDACTED:email] {"
    assert [(h.rule_id, h.path) for h in result.hits] == [("email", "")]


def test_a_redacted_line_is_written_in_claude_codes_compact_format(fakes: dict[str, str]) -> None:
    # Needed so that redacted attachment lines can still be de-duplicated (exact round trip).
    result = redact_line('{"a":1,"b":{"c":"' + fakes["email"] + '"}}', 1)
    assert result.text == '{"a":1,"b":{"c":"[REDACTED:email]"}}'  # no spaces after , or :


def test_unicode_text_is_preserved(fakes: dict[str, str]) -> None:
    result = redact_line(_line(t=f"नमस्ते {fakes['email']}"), 1)
    assert result.text is not None
    assert "नमस्ते" in result.text  # not turned into \u escapes


def test_a_line_that_fails_to_process_is_withheld_not_uploaded() -> None:
    def explode(_: str) -> bool:
        raise RuntimeError("simulated bug in a rule")

    broken = Rule("broken", "always matches, then fails", re.compile("secret"), validator=explode)
    result = redact_line('{"a": "a secret value"}', 1, rules=[broken])
    assert result.withheld
    assert result.text is None


def test_numbers_are_not_inspected_known_limit() -> None:
    # Documented limit: only strings are scanned.
    found: list[tuple[str, str]] = []
    assert redact_value({"phone": 9876543210}, "", [], found) == {"phone": 9876543210}
    result = redact_line('{"phone": 9876543210}', 1)
    assert result.text == '{"phone": 9876543210}'
    assert result.hits == []
