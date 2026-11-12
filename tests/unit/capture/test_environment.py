import json
from pathlib import Path

import pytest

from ccp.capture.environment import (
    MAX_TRANSCRIPT_LINES,
    HostInfo,
    collect_process_environment,
    collect_transcript_environment,
)
from ccp.environment import MAX_VALUE_LENGTH

HOST = HostInfo(
    os_name="Windows",
    os_version="11 10.0.26200",
    architecture="AMD64",
    hostname="laptop-1",
    user="dev",
)
ACCOUNT = {
    "oauthAccount": {
        "displayName": "Dev",
        "fullName": "Dev Eloper",
        "emailAddress": "dev@example.com",
        "organizationName": "Acme",
        "organizationUuid": "00000000-0000-0000-0000-000000000001",
        "billingType": "something",
    }
}


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / ".claude.json").write_text(json.dumps(ACCOUNT), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- the process stage


def test_the_process_record_has_machine_account_and_surface(home: Path) -> None:
    env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "MSYSTEM": "UCRT64"}
    record = collect_process_environment(env, home, HOST)
    assert record.to_payload() == {
        "entrypoint": "cli",
        "surface": "terminal",
        "hook_shell": "git-bash",
        "access_path": "local",
        "os_name": "Windows",
        "os_version": "11 10.0.26200",
        "cpu_architecture": "AMD64",
        "os_user": "dev",
        "hostname": "laptop-1",
        "account_name": "Dev",
        "account_email": "dev@example.com",
        "organisation_name": "Acme",
        "organisation_uuid": "00000000-0000-0000-0000-000000000001",
        "auth_type": "subscription",
    }


def test_the_vscode_extension_is_recognised_from_the_entrypoint(home: Path) -> None:
    record = collect_process_environment({"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}, home, HOST)
    assert (record.entrypoint, record.surface) == ("claude-vscode", "vscode")


def test_an_api_key_in_the_environment_means_api_key_auth_but_is_never_recorded(
    home: Path,
) -> None:
    secret = "not-a-real-" + "key-value"
    record = collect_process_environment({"ANTHROPIC_API_KEY": secret}, home, HOST)
    assert record.auth_type == "api_key"
    assert secret not in repr(record.to_payload())


def test_without_a_login_file_the_account_fields_stay_unknown(tmp_path: Path) -> None:
    record = collect_process_environment({}, tmp_path, HOST)
    assert record.account_email is None and record.auth_type is None
    assert record.os_name == "Windows"  # the rest is still collected


def test_a_broken_login_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    assert collect_process_environment({}, tmp_path, HOST).account_email is None


def test_the_full_name_is_used_when_there_is_no_display_name(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"fullName": "Dev Eloper"}}), encoding="utf-8"
    )
    assert collect_process_environment({}, tmp_path, HOST).account_name == "Dev Eloper"


def test_values_are_cleaned_to_single_short_lines(tmp_path: Path) -> None:
    messy = HostInfo("Win\ndows", "x" * 2000, "AMD64", "  host  ", "")
    record = collect_process_environment({}, tmp_path, messy)
    assert record.os_name == "Win dows"
    assert record.os_version is not None and len(record.os_version) == MAX_VALUE_LENGTH
    assert record.hostname == "host"
    assert record.os_user is None  # empty means unknown


def test_the_current_machine_can_be_described() -> None:
    host = HostInfo.current()
    assert host.os_name and host.architecture


# ---------------------------------------------------------------- the transcript stage


def _transcript(tmp_path: Path, *entries: object) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


ENVIRONMENT_ENTRY = {
    "type": "attachment",
    "attachment": {
        "type": "environment",
        "snapshot": {
            "shell": "PowerShell (primary); Bash tool also available",
            "osVersion": "Windows 11 Home Single Language 10.0.26200",
            "platform": "win32",
        },
    },
}


def test_the_version_and_tool_shell_are_read_from_the_transcript(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        {"type": "mode", "mode": "x"},
        {"type": "user", "version": "2.1.289", "message": {"content": "a prompt"}},
        ENVIRONMENT_ENTRY,
    )
    record = collect_transcript_environment(path)
    assert record.to_payload() == {
        "claude_code_version": "2.1.289",
        "tool_shell": "PowerShell (primary); Bash tool also available",
        "os_version": "Windows 11 Home Single Language 10.0.26200",
    }


def test_no_prompt_or_answer_text_is_read_or_returned(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path,
        {"type": "user", "version": "2.1.289", "message": {"content": "my secret prompt"}},
        ENVIRONMENT_ENTRY,
    )
    assert "secret prompt" not in repr(collect_transcript_environment(path))


def test_a_transcript_without_these_entries_gives_an_empty_record(tmp_path: Path) -> None:
    path = _transcript(tmp_path, {"type": "mode"}, [1, 2], "text")
    assert collect_transcript_environment(path).to_payload() == {}


def test_a_missing_or_garbled_transcript_gives_an_empty_record(tmp_path: Path) -> None:
    assert collect_transcript_environment(tmp_path / "nope.jsonl").to_payload() == {}
    (tmp_path / "bad.jsonl").write_text("not json\n{also not\n", encoding="utf-8")
    assert collect_transcript_environment(tmp_path / "bad.jsonl").to_payload() == {}


def test_only_the_start_of_a_long_transcript_is_examined(tmp_path: Path) -> None:
    filler = [{"type": "mode"}] * MAX_TRANSCRIPT_LINES
    path = _transcript(tmp_path, *filler, {"type": "user", "version": "9.9.9"})
    assert collect_transcript_environment(path).claude_code_version is None
