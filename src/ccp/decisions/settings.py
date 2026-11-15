"""The settings of the decision extraction, read from the environment and the ``.env`` file.

=================================  ===============  ==============================================
Variable                           Default          Meaning
=================================  ===============  ==============================================
``CCP_DECISION_MODEL``             claude-opus-5-5  the model that extracts and reviews decisions
``CCP_DECISION_TIMEOUT_SECONDS``   600              longest wait for one model call
``CCP_DECISION_MAX_CHARS``         80000            largest digest of a chat sent to the model
=================================  ===============  ==============================================
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ccp.settings import DEFAULT_ENV_FILE, SettingsError, read_env_file

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_CHARS = 80_000

# (variable, default, smallest, largest) of the whole-number settings.
_TIMEOUT = ("CCP_DECISION_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 10, 86_400)
_MAX_CHARS = ("CCP_DECISION_MAX_CHARS", DEFAULT_MAX_CHARS, 2_000, 2_000_000)


@dataclass(frozen=True)
class DecisionSettings:
    """How decisions are extracted."""

    model: str = DEFAULT_MODEL
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_chars: int = DEFAULT_MAX_CHARS


def load_decision_settings(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> DecisionSettings:
    """Read the settings; the environment takes precedence over the ``.env`` file.

    Args:
        env: The variables to read (default: ``os.environ``).
        env_file: A ``.env`` file read first, or None.

    Raises:
        SettingsError: A value is not a whole number or is out of range.
    """
    values: dict[str, str] = read_env_file(env_file) if env_file is not None else {}
    values.update(os.environ if env is None else env)
    errors: list[str] = []
    timeout = _whole_number(values, *_TIMEOUT, errors)
    max_chars = _whole_number(values, *_MAX_CHARS, errors)
    if errors:
        raise SettingsError("Invalid settings:\n- " + "\n- ".join(errors))
    model = values.get("CCP_DECISION_MODEL", "").strip() or DEFAULT_MODEL
    return DecisionSettings(model=model, timeout_seconds=timeout, max_chars=max_chars)


def _whole_number(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be a whole number")
        return default
    if not minimum <= value <= maximum:
        errors.append(f"{name} must be between {minimum} and {maximum}")
        return default
    return value
