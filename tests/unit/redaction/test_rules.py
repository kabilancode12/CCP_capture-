import pytest

from ccp.redaction import redact_string
from ccp.redaction.rules import DEFAULT_RULES

ALL_RULE_IDS = [rule.rule_id for rule in DEFAULT_RULES]

# Invented values, built from pieces so no complete fake credential sits in the source.
FAKE_VALUE = "abcd1234" + "efgh5678"
FAKE_PASSWORD = "hunter2" + "hunter2"


def test_every_rule_has_a_fake_value_to_test_it(fakes: dict[str, str]) -> None:
    # Rules that are tested by context (not by a single fake value) are listed here.
    tested_by_context = {"url-password", "bearer-token", "secret-assignment"}
    assert set(ALL_RULE_IDS) - tested_by_context == set(fakes)


@pytest.mark.parametrize(
    "rule_id", sorted(set(ALL_RULE_IDS) - {"url-password", "bearer-token", "secret-assignment"})
)
def test_each_fake_value_is_redacted_by_its_rule(rule_id: str, fakes: dict[str, str]) -> None:
    value = fakes[rule_id]
    cleaned, fired = redact_string(f"before {value} after")
    assert value not in cleaned
    assert f"[REDACTED:{rule_id}]" in cleaned
    assert fired == [rule_id]
    assert cleaned.startswith("before ") and cleaned.endswith(" after")


def test_private_key_without_end_marker_is_still_redacted(fakes: dict[str, str]) -> None:
    truncated = fakes["private-key"].split("\n-----END")[0]
    cleaned, fired = redact_string(f"key: {truncated}")
    assert "MIIBVQ" not in cleaned
    assert fired == ["private-key"]


# ---------------------------------------------------------------- rules that keep context


def test_assignment_keeps_the_name_and_hides_the_value() -> None:
    cleaned, fired = redact_string(f"db_password = {FAKE_PASSWORD}")
    assert cleaned == "db_password = [REDACTED:secret-assignment]"
    assert fired == ["secret-assignment"]


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "DB_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "secret_key",
        "api-key",
        "auth_token",
        "mypwd",
    ],
)
def test_assignment_is_redacted_for_common_variable_names(name: str) -> None:
    cleaned, fired = redact_string(f"{name}={FAKE_VALUE}")
    assert FAKE_VALUE not in cleaned
    assert fired == ["secret-assignment"]


def test_json_style_assignment_is_redacted() -> None:
    cleaned, _ = redact_string(f'{{"api_key": "{FAKE_VALUE}"}}')
    assert FAKE_VALUE not in cleaned


def test_url_password_is_hidden_but_user_and_host_stay() -> None:
    cleaned, fired = redact_string("postgres://ccp:s3cretpw@db.internal:5432/ccp")
    assert cleaned == "postgres://ccp:[REDACTED:url-password]@db.internal:5432/ccp"
    assert fired == ["url-password"]


def test_bearer_token_is_hidden() -> None:
    cleaned, fired = redact_string("Authorization: Bearer abcdefghij1234567890XYZ")
    assert cleaned == "Authorization: Bearer [REDACTED:bearer-token]"
    assert fired == ["bearer-token"]


def test_grouped_aadhaar_is_redacted(fakes: dict[str, str]) -> None:
    number = fakes["aadhaar"]
    grouped = f"{number[:4]} {number[4:8]} {number[8:]}"
    cleaned, fired = redact_string(f"id {grouped}")
    assert cleaned == "id [REDACTED:aadhaar]"
    assert fired == ["aadhaar"]


def test_mobile_with_country_code_is_redacted() -> None:
    cleaned, fired = redact_string("call +91 98765 43210 now")
    assert cleaned == "call [REDACTED:indian-mobile] now"
    assert fired == ["indian-mobile"]


# ---------------------------------------------------------------- things that must NOT be redacted


@pytest.mark.parametrize(
    "text",
    [
        "Just a normal sentence about passwords and tokens.",
        "password = short",  # value under 8 characters
        "tokenizer = standard_analyzer_v2",  # 'token' is only part of a longer word
        "max_tokens = 1024",  # value under 8 characters
        "token = [REDACTED:aws-access-key-id]",  # already redacted: not matched again
        "git clone git@github.com:org/repo.git",  # SSH remote, not a person
        "order 234567890123 shipped",  # 12 digits but fails the Aadhaar checksum
        "epoch 1731000000 ms",  # 10 digits but does not start with 6-9
        "code ABCDE1234F here",  # PAN-like but 4th letter is not a valid holder type
        "version 3.11.9 build 20260101",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    cleaned, fired = redact_string(text)
    assert cleaned == text
    assert fired == []


def test_uuid_whose_last_group_looks_like_an_aadhaar_is_left_alone(fakes: dict[str, str]) -> None:
    # A real bug found on real transcripts: the last 12-digit group of a UUID passed the
    # Aadhaar checksum and was redacted, breaking `uuid` / `parentUuid` links.
    uuid = f"3f2a9c1e-7b4d-4e8a-9c3b-{fakes['aadhaar']}"
    cleaned, fired = redact_string(f"id {uuid} end")
    assert cleaned == f"id {uuid} end"
    assert fired == []


def test_digits_inside_a_hash_or_longer_token_are_left_alone(fakes: dict[str, str]) -> None:
    for text in (
        f"sha deadbeef{fakes['aadhaar']}cafe",
        f"abc{fakes['indian-mobile']}",
        f"{fakes['indian-mobile']}abc",
    ):
        assert redact_string(text) == (text, []), text


def test_standalone_numbers_next_to_punctuation_are_still_redacted(fakes: dict[str, str]) -> None:
    # Underscores count as separators: a number inside user_<number> is still redacted.
    for template in ("({})", "id:{}.", "[{}]", '"{}"', "{},", "user_{}_x"):
        cleaned, fired = redact_string(template.format(fakes["aadhaar"]))
        assert fired == ["aadhaar"], template
        assert fakes["aadhaar"] not in cleaned


def test_several_values_in_one_string_are_all_redacted(fakes: dict[str, str]) -> None:
    text = f"{fakes['email']} used {fakes['aws-access-key-id']} and {fakes['pan']}"
    cleaned, fired = redact_string(text)
    assert sorted(fired) == ["aws-access-key-id", "email", "pan"]
    for value in (fakes["email"], fakes["aws-access-key-id"], fakes["pan"]):
        assert value not in cleaned


def test_redacting_twice_changes_nothing_more(fakes: dict[str, str]) -> None:
    once, _ = redact_string(f"{fakes['jwt']} {fakes['aadhaar']} password={FAKE_PASSWORD}")
    twice, fired = redact_string(once)
    assert twice == once
    assert fired == []
