"""Application settings, read from environment variables.

Every setting comes from the environment, so the same code runs on a laptop and on a server:
only the values change. For local development the variables can live in a git-ignored ``.env``
file (see ``.env.example`` and ``docs/configuration.md``).

Rules this module follows:

* **One place** reads configuration; the rest of the code receives a :class:`Settings` object.
* **All problems at once**: a missing or invalid variable does not stop at the first error,
  so the operator can fix everything in one go.
* **No values in messages**: errors name the variable, never its value.
* **Secrets are wrapped** in :class:`Secret`, which prints as ``***`` so a password cannot
  leak into a log line or traceback by accident.

Typical use::

    settings = load_settings()
    settings.database.host
    settings.api.ingest_token.reveal()   # only where the real value is needed
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path(".env")

# Strings accepted as true / false for boolean settings (compared in lower case).
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# Shortest accepted ingestion token: a short token is too easy to guess.
MIN_TOKEN_LENGTH = 16


class SettingsError(ValueError):
    """Raised when one or more settings are missing or invalid.

    The message lists every problem, one per line. It never contains a setting's value.
    """


class Secret:
    """A string that must not be printed or logged, such as a password or token.

    ``str()`` and ``repr()`` both give ``***``. Call :meth:`reveal` at the single place where
    the real value is needed (for example when connecting to the database).
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap ``value``."""
        self._value = value

    def reveal(self) -> str:
        """Return the real value. Use sparingly and never log the result."""
        return self._value

    def __repr__(self) -> str:
        """Return a masked representation."""
        return "Secret(***)"

    def __str__(self) -> str:
        """Return a masked string."""
        return "***"


@dataclass(frozen=True)
class DatabaseSettings:
    """PostgreSQL connection settings."""

    host: str
    port: int
    name: str
    user: str
    password: Secret


@dataclass(frozen=True)
class StorageSettings:
    """MinIO (S3-compatible object storage) settings."""

    endpoint: str  # "host:port", without a scheme
    access_key: str
    secret_key: Secret
    bucket: str
    secure: bool  # True = HTTPS


@dataclass(frozen=True)
class ApiSettings:
    """Ingestion API settings."""

    host: str
    port: int
    ingest_token: Secret  # developers' machines send this token with every upload
    session_idle_minutes: int  # a session with no activity this long is marked closed
    max_chunk_bytes: int  # largest chunk the API accepts


@dataclass(frozen=True)
class ClientSettings:
    """Settings of the capture client, which runs on a developer's machine.

    The client talks only to the ingestion API: it needs no database or storage credentials.
    """

    api_url: str  # base URL of the ingestion API, without a trailing slash
    token: Secret  # the bearer token (the same value as the server's CCP_INGEST_TOKEN)
    state_dir: Path  # where the client remembers how far each transcript has been uploaded
    chunk_bytes: int  # target size of one uploaded chunk (source bytes)
    timeout_seconds: float  # per HTTP request
    budget_seconds: float  # total time one sync may take (hooks have a time limit)
    snapshots_enabled: bool  # take code snapshots (hidden Git refs) of the working tree
    snapshot_remote: str | None  # Git URL of the capture project for the repository
    snapshot_remote_base: str | None  # or a group URL; the project is <base>/<repo>-capture.git


@dataclass(frozen=True)
class DashboardSettings:
    """Settings for setting up the dashboards in Metabase (``ccp-dashboards``)."""

    metabase_url: str  # where Metabase answers, without a trailing slash
    admin_email: str  # the Metabase administrator that is created on first setup
    admin_password: Secret
    database_name: str  # the database the dashboards read (POSTGRES_DB)
    database_host: str  # PostgreSQL as Metabase sees it (inside Compose: ``postgres``)
    database_port: int
    reader_user: str  # the read-only role made by ``ccp-db create-reader``
    reader_password: Secret


@dataclass(frozen=True)
class Settings:
    """All application settings."""

    """All application settings."""

    database: DatabaseSettings
    storage: StorageSettings
    api: ApiSettings


def read_env_file(path: Path) -> dict[str, str]:
    """Read ``KEY=value`` lines from a ``.env`` file.

    Blank lines and lines starting with ``#`` are ignored, and one pair of surrounding quotes
    is removed from a value. There is no variable expansion. A missing file gives ``{}``.

    Args:
        path: The file to read.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class _Reader:
    """Reads typed values from a mapping and remembers every problem found."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.errors: list[str] = []

    def text(self, name: str, default: str | None = None) -> str:
        """Read a string. Without a ``default`` the variable is required."""
        value = self._env.get(name, "").strip()
        if value:
            return value
        if default is not None:
            return default
        self.errors.append(f"{name} is required")
        return ""

    def secret(self, name: str, min_length: int = 1) -> Secret:
        """Read a required secret of at least ``min_length`` characters."""
        value = self.text(name)
        if value and len(value) < min_length:
            self.errors.append(f"{name} must be at least {min_length} characters long")
        return Secret(value)

    def integer(self, name: str, default: int, minimum: int, maximum: int) -> int:
        """Read an integer between ``minimum`` and ``maximum`` (inclusive)."""
        raw = self._env.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            self.errors.append(f"{name} must be a whole number")
            return default
        if not minimum <= value <= maximum:
            self.errors.append(f"{name} must be between {minimum} and {maximum}")
            return default
        return value

    def boolean(self, name: str, default: bool) -> bool:
        """Read a boolean: true/false, yes/no, on/off or 1/0."""
        raw = self._env.get(name, "").strip().lower()
        if not raw:
            return default
        if raw in _TRUE_VALUES:
            return True
        if raw in _FALSE_VALUES:
            return False
        self.errors.append(f"{name} must be true or false")
        return default

    def raise_if_errors(self) -> None:
        """Raise :class:`SettingsError` listing every problem found, if there were any."""
        if self.errors:
            raise SettingsError("Invalid settings:\n- " + "\n- ".join(self.errors))


def _merge_env(env: Mapping[str, str] | None, env_file: Path | None) -> dict[str, str]:
    """Combine the ``.env`` file and the environment; the environment takes precedence."""
    merged: dict[str, str] = {}
    if env_file is not None:
        merged.update(read_env_file(env_file))
    merged.update(os.environ if env is None else env)
    return merged


def _read_database(reader: _Reader) -> DatabaseSettings:
    """Read the PostgreSQL group of settings."""
    return DatabaseSettings(
        host=reader.text("POSTGRES_HOST", "127.0.0.1"),
        port=reader.integer("POSTGRES_PORT", 5432, 1, 65535),
        name=reader.text("POSTGRES_DB"),
        user=reader.text("POSTGRES_USER"),
        password=reader.secret("POSTGRES_PASSWORD"),
    )


def _read_storage(reader: _Reader) -> StorageSettings:
    """Read the MinIO group of settings."""
    return StorageSettings(
        endpoint=reader.text("MINIO_ENDPOINT", "127.0.0.1:9000"),
        access_key=reader.text("MINIO_ACCESS_KEY"),
        secret_key=reader.secret("MINIO_SECRET_KEY", min_length=8),
        bucket=reader.text("MINIO_BUCKET", "ccp-raw"),
        secure=reader.boolean("MINIO_SECURE", default=False),
    )


def load_database_settings(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> DatabaseSettings:
    """Build only the database settings.

    Tools that need just the database (such as the migration runner) use this, so they do not
    demand the storage and API variables. Arguments and errors are as in :func:`load_settings`.
    """
    reader = _Reader(_merge_env(env, env_file))
    database = _read_database(reader)
    reader.raise_if_errors()
    return database


def load_reader_password(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> Secret:
    """Read the password for the dashboards' read-only database role (``CCP_READER_PASSWORD``).

    Raises:
        SettingsError: If it is missing or shorter than 12 characters.
    """
    reader = _Reader(_merge_env(env, env_file))
    password = reader.secret("CCP_READER_PASSWORD", min_length=12)
    reader.raise_if_errors()
    return password


def load_dashboard_settings(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> DashboardSettings:
    """Build the settings for ``ccp-dashboards`` (Metabase address, administrator, read-only role).

    Raises:
        SettingsError: If anything is missing or invalid; the message lists every problem.
    """
    merged = _merge_env(env, env_file)
    reader = _Reader(merged)
    port = merged.get("METABASE_PORT", "").strip() or "3000"
    settings = DashboardSettings(
        metabase_url=reader.text("METABASE_URL", f"http://127.0.0.1:{port}").rstrip("/"),
        admin_email=reader.text("CCP_METABASE_ADMIN_EMAIL"),
        admin_password=reader.secret("CCP_METABASE_ADMIN_PASSWORD", min_length=12),
        database_name=reader.text("POSTGRES_DB"),
        database_host=reader.text("CCP_METABASE_DB_HOST", "postgres"),
        database_port=reader.integer("CCP_METABASE_DB_PORT", 5432, 1, 65535),
        reader_user=reader.text("CCP_READER_USER", "ccp_reader"),
        reader_password=reader.secret("CCP_READER_PASSWORD", min_length=12),
    )
    reader.raise_if_errors()
    return settings


def load_client_settings(env: Mapping[str, str] | None = None) -> ClientSettings:
    """Build the capture client's settings from environment variables only.

    Unlike the server settings, **no ``.env`` file is read**. The client runs inside whatever
    project the developer is working in, and that project could contain a ``.env`` file: reading
    it would let an untrusted repository point the client (and its token) at another server.

    Args:
        env: Variables to read. Defaults to ``os.environ``.

    Raises:
        SettingsError: If a variable is missing or invalid; the message lists all of them.
    """
    reader = _Reader(dict(os.environ if env is None else env))
    api_url = reader.text("CCP_API_URL", "http://127.0.0.1:8000").rstrip("/")
    if not api_url.lower().startswith(("http://", "https://")):
        reader.errors.append("CCP_API_URL must start with http:// or https://")
    state_dir = reader.text("CCP_STATE_DIR", str(Path.home() / ".ccp" / "capture"))
    snapshot_remote = reader.text("CCP_SNAPSHOT_REMOTE", "") or None
    snapshot_remote_base = reader.text("CCP_SNAPSHOT_REMOTE_BASE", "") or None
    # Snapshots write hidden refs into the developer's repository, so they are opt-in: they run
    # when a capture project is configured, or when CCP_SNAPSHOTS=true asks for local-only refs.
    snapshots_enabled = reader.boolean(
        "CCP_SNAPSHOTS", default=bool(snapshot_remote or snapshot_remote_base)
    )
    settings = ClientSettings(
        api_url=api_url,
        token=reader.secret("CCP_INGEST_TOKEN", min_length=MIN_TOKEN_LENGTH),
        state_dir=Path(state_dir),
        chunk_bytes=reader.integer("CCP_CLIENT_CHUNK_BYTES", 1024 * 1024, 1024, 8 * 1024 * 1024),
        timeout_seconds=float(reader.integer("CCP_CLIENT_TIMEOUT_SECONDS", 15, 1, 120)),
        budget_seconds=float(reader.integer("CCP_CLIENT_BUDGET_SECONDS", 20, 1, 600)),
        snapshots_enabled=snapshots_enabled,
        snapshot_remote=snapshot_remote,
        snapshot_remote_base=snapshot_remote_base,
    )
    reader.raise_if_errors()
    return settings


def load_storage_settings(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> StorageSettings:
    """Build only the storage (MinIO) settings. Arguments and errors as in :func:`load_settings`."""
    reader = _Reader(_merge_env(env, env_file))
    storage = _read_storage(reader)
    reader.raise_if_errors()
    return storage


def load_settings(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> Settings:
    """Build :class:`Settings` from the environment.

    Real environment variables take precedence over values in the ``.env`` file.

    Args:
        env: Variables to read. Defaults to ``os.environ`` (tests pass their own mapping and
            ``env_file=None`` so nothing outside the test is read).
        env_file: A ``.env`` file to read first, or ``None`` to skip it.

    Returns:
        The validated settings.

    Raises:
        SettingsError: If any variable is missing or invalid. The message lists all of them.
    """
    reader = _Reader(_merge_env(env, env_file))
    settings = Settings(
        database=_read_database(reader),
        storage=_read_storage(reader),
        api=ApiSettings(
            host=reader.text("CCP_API_HOST", "127.0.0.1"),
            port=reader.integer("CCP_API_PORT", 8000, 1, 65535),
            ingest_token=reader.secret("CCP_INGEST_TOKEN", min_length=MIN_TOKEN_LENGTH),
            session_idle_minutes=reader.integer("CCP_SESSION_IDLE_MINUTES", 30, 1, 1440),
            max_chunk_bytes=reader.integer(
                "CCP_MAX_CHUNK_BYTES", 8 * 1024 * 1024, 1024, 64 * 1024 * 1024
            ),
        ),
    )
    reader.raise_if_errors()
    return settings
