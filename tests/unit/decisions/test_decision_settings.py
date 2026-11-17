"""The settings of the decision extraction."""

from pathlib import Path

import pytest

from ccp.decisions.settings import DEFAULT_MAX_CHARS, DEFAULT_MODEL, load_decision_settings
from ccp.settings import SettingsError


def test_defaults() -> None:
    settings = load_decision_settings({}, env_file=None)
    assert settings.model == DEFAULT_MODEL == "claude-opus-5-5"
    assert settings.timeout_seconds == 600
    assert settings.max_chars == DEFAULT_MAX_CHARS == 200_000


def test_values_are_read_from_the_environment() -> None:
    settings = load_decision_settings(
        {
            "CCP_DECISION_MODEL": " claude-sonnet-5-5 ",
            "CCP_DECISION_TIMEOUT_SECONDS": "30",
            "CCP_DECISION_MAX_CHARS": "5000",
        },
        env_file=None,
    )
    assert (settings.model, settings.timeout_seconds, settings.max_chars) == (
        "claude-sonnet-5-5",
        30,
        5000,
    )


def test_the_env_file_is_read_and_the_environment_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CCP_DECISION_MODEL=from-file\nCCP_DECISION_MAX_CHARS=9000\n")
    settings = load_decision_settings({"CCP_DECISION_MODEL": "from-env"}, env_file=env_file)
    assert settings.model == "from-env"
    assert settings.max_chars == 9000


@pytest.mark.parametrize("variable", ["CCP_DECISION_TIMEOUT_SECONDS", "CCP_DECISION_MAX_CHARS"])
@pytest.mark.parametrize("value", ["abc", "0", "-5", "99999999999"])
def test_bad_numbers_are_reported(variable: str, value: str) -> None:
    with pytest.raises(SettingsError, match=variable):
        load_decision_settings({variable: value}, env_file=None)
