from pathlib import Path

import pytest

from ccp.explorer.cli import build_parser, load_secret
from ccp.settings import SettingsError


def test_the_secret_comes_from_the_environment_or_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CCP_EXPLORER_SECRET=file-secret-long-enough\n")
    assert load_secret({}, env_file) == "file-secret-long-enough"
    assert load_secret({"CCP_EXPLORER_SECRET": "env-secret-long-enough"}, env_file) == (
        "env-secret-long-enough"
    )


@pytest.mark.parametrize("value", [None, "", "short"])
def test_a_missing_or_short_secret_is_refused(value: str | None) -> None:
    env = {} if value is None else {"CCP_EXPLORER_SECRET": value}
    with pytest.raises(SettingsError, match="CCP_EXPLORER_SECRET"):
        load_secret(env, None)


def test_create_user_needs_a_known_role() -> None:
    parser = build_parser()
    args = parser.parse_args(["create-user", "a@example.com", "--role", "lead"])
    assert (args.email, args.role, args.account_email) == ("a@example.com", "lead", None)
    with pytest.raises(SystemExit):
        parser.parse_args(["create-user", "a@example.com", "--role", "boss"])
    assert parser.parse_args(["serve"]).port == 8090
