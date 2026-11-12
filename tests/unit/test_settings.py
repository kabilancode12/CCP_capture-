from pathlib import Path

import pytest

from ccp.settings import (
    MIN_TOKEN_LENGTH,
    Secret,
    SettingsError,
    load_client_settings,
    load_database_settings,
    load_settings,
    load_storage_settings,
    read_env_file,
)

# A complete, valid set of required variables. Tests change one value at a time.
VALID_ENV = {
    "POSTGRES_DB": "ccp",
    "POSTGRES_USER": "ccp",
    "POSTGRES_PASSWORD": "pg-password-value",
    "MINIO_ACCESS_KEY": "ccp-admin",
    "MINIO_SECRET_KEY": "minio-secret-value",
    "CCP_INGEST_TOKEN": "t" * MIN_TOKEN_LENGTH,
}


def _load(**overrides: str) -> object:
    """Load settings from VALID_ENV plus overrides; an empty string removes a variable."""
    env = {**VALID_ENV, **overrides}
    env = {key: value for key, value in env.items() if value != ""}
    return load_settings(env, env_file=None)


def test_defaults_are_applied_for_optional_variables() -> None:
    settings = load_settings(VALID_ENV, env_file=None)
    assert settings.database.host == "127.0.0.1"
    assert settings.database.port == 5432
    assert settings.storage.endpoint == "127.0.0.1:9000"
    assert settings.storage.bucket == "ccp-raw"
    assert settings.storage.secure is False
    assert settings.api.port == 8000
    assert settings.api.session_idle_minutes == 30
    assert settings.api.max_chunk_bytes == 8 * 1024 * 1024


def test_values_from_the_environment_override_defaults() -> None:
    env = {**VALID_ENV, "POSTGRES_PORT": "6543", "MINIO_SECURE": "yes", "CCP_API_PORT": "9000"}
    settings = load_settings(env, env_file=None)
    assert settings.database.port == 6543
    assert settings.storage.secure is True
    assert settings.api.port == 9000


def test_all_missing_variables_are_reported_together() -> None:
    with pytest.raises(SettingsError) as error:
        load_settings({}, env_file=None)
    message = str(error.value)
    for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "MINIO_ACCESS_KEY"):
        assert f"{name} is required" in message
    assert "CCP_INGEST_TOKEN is required" in message


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("POSTGRES_PORT", "abc", "POSTGRES_PORT must be a whole number"),
        ("POSTGRES_PORT", "70000", "POSTGRES_PORT must be between 1 and 65535"),
        ("MINIO_SECURE", "maybe", "MINIO_SECURE must be true or false"),
        ("CCP_SESSION_IDLE_MINUTES", "0", "CCP_SESSION_IDLE_MINUTES must be between 1 and 1440"),
        ("CCP_INGEST_TOKEN", "short", "CCP_INGEST_TOKEN must be at least 16 characters long"),
        ("MINIO_SECRET_KEY", "short", "MINIO_SECRET_KEY must be at least 8 characters long"),
    ],
)
def test_invalid_values_are_rejected_with_a_clear_message(
    name: str, value: str, expected: str
) -> None:
    with pytest.raises(SettingsError, match=expected):
        _load(**{name: value})


def test_error_messages_never_contain_leaked_texts() -> None:
    leaked_text = "very-secret-but-too-short"
    env = {**VALID_ENV, "POSTGRES_PORT": leaked_text, "CCP_INGEST_TOKEN": "tiny"}
    with pytest.raises(SettingsError) as error:
        load_settings(env, env_file=None)
    assert leaked_text not in str(error.value)
    assert "tiny" not in str(error.value)


def test_secrets_are_masked_when_printed() -> None:
    settings = load_settings(VALID_ENV, env_file=None)
    assert "pg-password-value" not in repr(settings)
    assert "minio-secret-value" not in repr(settings)
    assert str(settings.database.password) == "***"
    assert settings.database.password.reveal() == "pg-password-value"


def test_secret_wrapper_masks_str_and_repr() -> None:
    secret = Secret("abc123")
    assert str(secret) == "***"
    assert repr(secret) == "Secret(***)"
    assert "abc123" not in f"{secret} {secret!r}"


def test_settings_are_immutable() -> None:
    settings = load_settings(VALID_ENV, env_file=None)
    with pytest.raises(AttributeError):
        settings.api.port = 1  # type: ignore[misc]


def test_blank_values_count_as_missing() -> None:
    with pytest.raises(SettingsError, match="POSTGRES_DB is required"):
        load_settings({**VALID_ENV, "POSTGRES_DB": "   "}, env_file=None)


# ---------------------------------------------------------------- .env file


def test_env_file_is_parsed(tmp_path: Path) -> None:
    file = tmp_path / ".env"
    file.write_text(
        "# comment\n\nA=1\nB = two\nC=\"quoted value\"\nD='single'\nnot a pair\nE=a=b\n",
        encoding="utf-8",
    )
    assert read_env_file(file) == {
        "A": "1",
        "B": "two",
        "C": "quoted value",
        "D": "single",
        "E": "a=b",
    }


def test_missing_env_file_gives_an_empty_result(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "missing.env") == {}


def test_real_environment_overrides_the_env_file(tmp_path: Path) -> None:
    file = tmp_path / ".env"
    file.write_text("POSTGRES_DB=from-file\nPOSTGRES_USER=from-file\n", encoding="utf-8")
    env = {**VALID_ENV, "POSTGRES_DB": "from-environment"}
    settings = load_settings(env, env_file=file)
    assert settings.database.name == "from-environment"  # environment wins
    assert settings.database.user == "ccp"  # also set in the environment


def test_values_missing_from_the_environment_come_from_the_file(tmp_path: Path) -> None:
    file = tmp_path / ".env"
    file.write_text("POSTGRES_PORT=7000\n", encoding="utf-8")
    assert load_settings(VALID_ENV, env_file=file).database.port == 7000


def test_database_settings_can_be_loaded_alone() -> None:
    env = {k: v for k, v in VALID_ENV.items() if k.startswith("POSTGRES_")}
    database = load_database_settings(env, env_file=None)
    assert (database.name, database.user, database.port) == ("ccp", "ccp", 5432)


def test_database_settings_report_their_own_missing_variables_only() -> None:
    with pytest.raises(SettingsError) as error:
        load_database_settings({}, env_file=None)
    assert "POSTGRES_DB is required" in str(error.value)
    assert "MINIO" not in str(error.value)


def test_storage_settings_can_be_loaded_alone() -> None:
    env = {k: v for k, v in VALID_ENV.items() if k.startswith("MINIO_")}
    storage = load_storage_settings(env, env_file=None)
    assert (storage.bucket, storage.endpoint, storage.secure) == (
        "ccp-raw",
        "127.0.0.1:9000",
        False,
    )


def test_storage_settings_report_their_own_missing_variables_only() -> None:
    with pytest.raises(SettingsError) as error:
        load_storage_settings({}, env_file=None)
    assert "MINIO_ACCESS_KEY is required" in str(error.value)
    assert "POSTGRES" not in str(error.value)


# ---------------------------------------------------------------- the capture client's settings


CLIENT_ENV = {"CCP_INGEST_TOKEN": "t" * MIN_TOKEN_LENGTH}


def test_client_settings_defaults() -> None:
    settings = load_client_settings(CLIENT_ENV)
    assert settings.api_url == "http://127.0.0.1:8000"
    assert settings.state_dir == Path.home() / ".ccp" / "capture"
    assert (settings.chunk_bytes, settings.timeout_seconds, settings.budget_seconds) == (
        1024 * 1024,
        15.0,
        20.0,
    )
    assert str(settings.token) == "***"


def test_client_settings_from_the_environment() -> None:
    env = {
        **CLIENT_ENV,
        "CCP_API_URL": "https://ccp.example.com/",
        "CCP_STATE_DIR": "/somewhere/else",
        "CCP_CLIENT_CHUNK_BYTES": "4096",
    }
    settings = load_client_settings(env)
    assert settings.api_url == "https://ccp.example.com"  # trailing slash removed
    assert settings.state_dir == Path("/somewhere/else")
    assert settings.chunk_bytes == 4096


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("CCP_API_URL", "ftp://x", "CCP_API_URL must start with http"),
        ("CCP_CLIENT_CHUNK_BYTES", "10", "CCP_CLIENT_CHUNK_BYTES must be between"),
        ("CCP_CLIENT_TIMEOUT_SECONDS", "abc", "CCP_CLIENT_TIMEOUT_SECONDS must be a whole number"),
        ("CCP_INGEST_TOKEN", "short", "CCP_INGEST_TOKEN must be at least 16"),
    ],
)
def test_invalid_client_settings_are_rejected(name: str, value: str, expected: str) -> None:
    with pytest.raises(SettingsError, match=expected):
        load_client_settings({**CLIENT_ENV, name: value})


def test_the_client_never_reads_an_env_file_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository's own .env must not be able to redirect the client or its token."""
    (tmp_path / ".env").write_text("CCP_API_URL=https://evil.example\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_client_settings(CLIENT_ENV).api_url == "http://127.0.0.1:8000"
