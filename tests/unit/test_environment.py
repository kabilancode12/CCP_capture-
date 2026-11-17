from typing import Any

import pytest

from ccp.environment import (
    FIELD_NAMES,
    LEGACY_FIELD_NAMES,
    MAX_VALUE_LENGTH,
    EnvironmentRecord,
    EnvironmentRecordError,
    parse_environment,
)


def test_a_record_lists_only_the_known_fields() -> None:
    record = EnvironmentRecord(surface="terminal", hostname="laptop-1")
    assert record.to_payload() == {"surface": "terminal", "hostname": "laptop-1"}
    assert EnvironmentRecord().to_payload() == {}


def test_a_later_record_replaces_known_fields_and_keeps_the_rest() -> None:
    first = EnvironmentRecord(surface="terminal", os_name="Windows")
    later = EnvironmentRecord(surface="vscode", claude_code_version="2.1.289")
    assert first.merged_with(later).to_payload() == {
        "surface": "vscode",  # replaced
        "os_name": "Windows",  # kept
        "claude_code_version": "2.1.289",  # added
    }


def test_the_field_names_match_the_database_columns() -> None:
    # session_env in migrations 001 and 016 (plus the key column); a mismatch would break the upsert.
    assert set(LEGACY_FIELD_NAMES) == {
        "claude_code_version", "entrypoint", "surface", "run_mode", "tool_shell", "hook_shell",
        "os_name", "os_version", "cpu_architecture", "access_path", "os_user", "hostname",
        "account_name", "account_email", "organisation_name", "organisation_uuid", "auth_type",
    }  # fmt: skip
    assert set(FIELD_NAMES) - set(LEGACY_FIELD_NAMES) == {
        "account_uuid", "full_name", "organisation_role", "machine_key", "folder", "git_remote",
        "git_user_name", "git_user_email",
    }  # fmt: skip


def test_a_record_can_be_restricted_to_the_fields_an_older_server_knows() -> None:
    record = EnvironmentRecord(surface="terminal", account_uuid="u-1", git_remote="x")
    assert record.restricted_to(LEGACY_FIELD_NAMES) == EnvironmentRecord(surface="terminal")


def test_a_valid_payload_is_parsed() -> None:
    record = parse_environment({"surface": "vscode", "os_user": "dev"})
    assert record == EnvironmentRecord(surface="vscode", os_user="dev")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        "text",
        {"surface": 5},
        {"surface": ""},
        {"surface": "x" * (MAX_VALUE_LENGTH + 1)},
        {"surface": "line\nbreak"},
        {"surface": "nul\x00"},
        {"not_a_field": "x"},
    ],
)
def test_malformed_payloads_are_rejected(payload: Any) -> None:
    with pytest.raises(EnvironmentRecordError):
        parse_environment(payload)


def test_error_messages_do_not_repeat_the_value_or_unknown_field_names() -> None:
    leaked = "do-not-echo-me"
    with pytest.raises(EnvironmentRecordError) as error:
        parse_environment({leaked: "x"})
    assert leaked not in str(error.value)
    with pytest.raises(EnvironmentRecordError) as error:
        parse_environment({"surface": leaked + "\n"})
    assert leaked not in str(error.value)
