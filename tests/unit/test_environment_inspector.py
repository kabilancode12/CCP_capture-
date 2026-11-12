import json
from pathlib import Path

from ccp.verify.environment_inspector import (
    detect_access_path,
    detect_shell,
    detect_surface,
    read_account_fields,
)

NO_CONTAINER = Path("does-not-exist")


def test_vscode_surface_from_term_program() -> None:
    assert detect_surface({"TERM_PROGRAM": "vscode"})["value"] == "vscode"


def test_jetbrains_surface() -> None:
    assert detect_surface({"TERMINAL_EMULATOR": "JetBrains-JediTerm"})["value"] == "jetbrains"


def test_default_surface_is_terminal() -> None:
    assert detect_surface({})["value"] == "terminal"


def test_ssh_access_path() -> None:
    assert detect_access_path({"SSH_CONNECTION": "a b c d"}, NO_CONTAINER)["value"] == "ssh"


def test_container_access_path_from_marker_file(tmp_path: Path) -> None:
    marker = tmp_path / ".dockerenv"
    marker.write_text("", encoding="utf-8")
    assert detect_access_path({}, marker)["value"] == "container"


def test_shell_detection_order() -> None:
    assert detect_shell({"WSL_DISTRO_NAME": "Ubuntu"})["value"] == "wsl"
    assert detect_shell({"MSYSTEM": "MINGW64"})["value"] == "git-bash"
    assert detect_shell({"SHELL": "/usr/bin/zsh"})["value"] == "zsh"


def test_account_values_are_hidden(tmp_path: Path) -> None:
    config = {"oauthAccount": {"emailAddress": "dev@example.com", "organizationName": "Acme"}}
    (tmp_path / ".claude.json").write_text(json.dumps(config), encoding="utf-8")

    result = read_account_fields(tmp_path, {"ANTHROPIC_API_KEY": "not-a-real-key"})

    assert result["oauth_account_fields"] == {
        "emailAddress": "<str len=15>",
        "organizationName": "<str len=4>",
    }
    assert result["api_key_in_environment"] is True
    assert "dev@example.com" not in json.dumps(result)
    assert "not-a-real-key" not in json.dumps(result)


def test_missing_config_file_is_reported(tmp_path: Path) -> None:
    result = read_account_fields(tmp_path, {})
    assert result["config_file_exists"] is False
    assert result["oauth_account_fields"] is None
