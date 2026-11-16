"""Install the capture hooks into Claude Code's ``settings.json``, and check that it all works.

``ccp-capture install --api-url URL --token TOKEN`` merges into ``~/.claude/settings.json``:

* the environment variables ``CCP_API_URL``, ``CCP_INGEST_TOKEN`` and ``CCP_SNAPSHOTS``, and
* the eight capture hooks (see :data:`ccp.capture.hooks.HOOK_TIMEOUTS`), each running
  ``"<this Python>" -m ccp.capture hook``.

Rules this module keeps, because the file belongs to the developer:

* A copy ``settings.json.bak-<timestamp>`` is made before anything is changed.
* A file that is not valid JSON (or not a JSON object) is **refused**, never overwritten.
* Installing twice gives the same file: our own hooks are recognised by their command and replaced,
  never duplicated. Every other setting and every hook of other tools is kept as it was.
* ``uninstall`` removes only our hooks and our three environment variables.
* The file is written atomically, and (on Linux and macOS) readable by its owner only, as it holds
  the token.

The token is never printed or logged. Standard library only; works on Windows and Linux.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ccp.capture.hook_settings import hook_command
from ccp.capture.hooks import HOOK_MATCHERS, HOOK_TIMEOUTS
from ccp.capture.state import StateStore
from ccp.settings import MIN_TOKEN_LENGTH
from ccp.storage.keys import MAIN_STREAM

ENV_API_URL = "CCP_API_URL"
ENV_TOKEN = "CCP_INGEST_TOKEN"  # noqa: S105  (the name of a variable, not a secret)
ENV_SNAPSHOTS = "CCP_SNAPSHOTS"
OUR_ENV_KEYS = (ENV_API_URL, ENV_TOKEN, ENV_SNAPSHOTS)

# A hook command is ours if it ends like this (the Python path in front of it varies).
_OUR_COMMAND_ENDINGS = ("-m ccp.capture hook", "ccp-capture hook")

# A request to the API that needs the token but changes nothing: a lifecycle event for a session
# id that is not valid. The API checks the token first (401 if wrong), then the id (400).
_TOKEN_PROBE_PATH = "/v1/sessions/-/lifecycle"  # noqa: S105  (a URL path, not a secret)
_PROBE_TIMEOUT_SECONDS = 5.0


class InstallError(RuntimeError):
    """The settings file or the given values cannot be used; the message says what to fix."""


def default_settings_path() -> Path:
    """Return Claude Code's user settings file, ``~/.claude/settings.json``."""
    return Path.home() / ".claude" / "settings.json"


@dataclass(frozen=True)
class InstallResult:
    """What :func:`install` or :func:`uninstall` did.

    Attributes:
        changed: False if the file already was as wanted (nothing was written).
        backup: The backup file made before writing, or ``None`` if there was nothing to back up.
    """

    changed: bool
    backup: Path | None


# ---------------------------------------------------------------- reading and writing


def _read_settings(path: Path) -> dict[str, Any]:
    """Read the settings file; an absent or empty file is an empty settings object.

    Raises:
        InstallError: If the file is not valid JSON, or not a JSON object. Nothing is changed.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        raise InstallError(f"cannot read {path}: {type(error).__name__}") from error
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as error:
        raise InstallError(
            f"{path} is not valid JSON ({error}). Nothing was changed; fix or move the file."
        ) from error
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object. Nothing was changed.")
    return data


def _write_settings(path: Path, data: dict[str, Any]) -> Path | None:
    """Back up the current file, then write ``data`` atomically. Returns the backup, if any."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
        _owner_only(backup)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _owner_only(temporary)
    os.replace(temporary, path)
    return backup


def _owner_only(path: Path) -> None:
    """Make a file readable by its owner only (it holds the token). Ignored where unsupported."""
    with contextlib.suppress(OSError):
        path.chmod(0o600)


# ---------------------------------------------------------------- our hooks


def _is_ours(hook: object) -> bool:
    """True if one hook entry (``{"type": "command", "command": ...}``) is a capture hook."""
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    return isinstance(command, str) and command.rstrip().endswith(_OUR_COMMAND_ENDINGS)


def _without_our_hooks(groups: object) -> list[Any]:
    """Return an event's hook groups with our hooks taken out; groups left empty are dropped."""
    kept: list[Any] = []
    for group in groups if isinstance(groups, list) else []:
        inner = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(inner, list):
            kept.append(group)  # not a shape we know: leave it alone
            continue
        others = [hook for hook in inner if not _is_ours(hook)]
        if others:
            kept.append({**group, "hooks": others})
    return kept


def _hooks_block(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Return the ``hooks`` object of the settings (an empty one if absent)."""
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f'"hooks" in {path} is not an object. Nothing was changed.')
    return hooks


def _env_block(data: dict[str, Any], path: Path) -> dict[str, Any]:
    """Return the ``env`` object of the settings (an empty one if absent)."""
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise InstallError(f'"env" in {path} is not an object. Nothing was changed.')
    return env


# ---------------------------------------------------------------- install, uninstall


def install(
    settings_path: Path,
    api_url: str,
    token: str,
    *,
    command: str | None = None,
    snapshots: bool = True,
) -> InstallResult:
    """Add (or update) the capture environment variables and the eight hooks.

    Args:
        settings_path: Claude Code's ``settings.json`` (tests pass a temporary file).
        api_url: The ingestion API address.
        token: The ingest token.
        command: The hook command; defaults to ``"<this Python>" -m ccp.capture hook``.
        snapshots: Value of ``CCP_SNAPSHOTS``.

    Raises:
        InstallError: If the address or token is unusable, or the file is not valid JSON.
    """
    api_url = api_url.strip().rstrip("/")
    if not api_url.lower().startswith(("http://", "https://")):
        raise InstallError("the API address must start with http:// or https://")
    if len(token.strip()) < MIN_TOKEN_LENGTH:
        raise InstallError(f"the token must be at least {MIN_TOKEN_LENGTH} characters")
    data = _read_settings(settings_path)
    before = json.dumps(data, sort_keys=True)

    env = _env_block(data, settings_path)
    env.update(
        {
            ENV_API_URL: api_url,
            ENV_TOKEN: token.strip(),
            ENV_SNAPSHOTS: "true" if snapshots else "false",
        }
    )
    data["env"] = env
    # Claude's reasoning is only written to the transcript when this is on; a value the developer
    # chose is left alone.
    data.setdefault("showThinkingSummaries", True)

    hooks = _hooks_block(data, settings_path)
    ours = command or hook_command()
    for event, timeout in HOOK_TIMEOUTS.items():
        groups = _without_our_hooks(hooks.get(event))
        group: dict[str, Any] = {
            "hooks": [{"type": "command", "command": ours, "timeout": timeout}]
        }
        if event in HOOK_MATCHERS:  # tool hooks only for the tools that can change files
            group = {"matcher": HOOK_MATCHERS[event], **group}
        groups.append(group)
        hooks[event] = groups
    data["hooks"] = hooks

    if json.dumps(data, sort_keys=True) == before:
        return InstallResult(changed=False, backup=None)
    return InstallResult(changed=True, backup=_write_settings(settings_path, data))


def uninstall(settings_path: Path) -> InstallResult:
    """Remove our hooks and our three environment variables; keep everything else.

    A file that does not exist is left alone. Events and blocks that become empty because of
    this are removed, so the file looks as it did before installing.

    Raises:
        InstallError: If the file is not valid JSON.
    """
    if not settings_path.is_file():
        return InstallResult(changed=False, backup=None)
    data = _read_settings(settings_path)
    before = json.dumps(data, sort_keys=True)

    hooks = _hooks_block(data, settings_path)
    for event in list(hooks):
        groups = _without_our_hooks(hooks[event])
        if groups:
            hooks[event] = groups
        elif _has_our_hook(hooks[event]):
            del hooks[event]
    if "hooks" in data and not hooks:
        del data["hooks"]

    env = _env_block(data, settings_path)
    for key in OUR_ENV_KEYS:
        env.pop(key, None)
    if "env" in data and not env:
        del data["env"]

    if json.dumps(data, sort_keys=True) == before:
        return InstallResult(changed=False, backup=None)
    return InstallResult(changed=True, backup=_write_settings(settings_path, data))


def _has_our_hook(groups: object) -> bool:
    """True if any group of an event contains one of our hooks."""
    return any(
        _is_ours(hook)
        for group in (groups if isinstance(groups, list) else [])
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"]
    )


def installed_events(settings_path: Path) -> list[str]:
    """Return the events (of the six) that have a capture hook in the file."""
    try:
        hooks = _hooks_block(_read_settings(settings_path), settings_path)
    except InstallError:
        return []
    return [event for event in HOOK_TIMEOUTS if _has_our_hook(hooks.get(event))]


def installed_env(settings_path: Path) -> dict[str, str]:
    """Return our environment variables as written in the file (empty if unreadable)."""
    try:
        env = _env_block(_read_settings(settings_path), settings_path)
    except InstallError:
        return {}
    return {key: str(env[key]) for key in OUR_ENV_KEYS if key in env}


def effective_env(settings_path: Path, process_env: Mapping[str, str]) -> dict[str, str]:
    """The variables the client would see: the installed ones, overridden by the shell's own.

    ``backfill`` and ``status`` run in a terminal, where Claude Code's ``env`` block does not
    apply; this gives them the same settings the hooks use.
    """
    return {**installed_env(settings_path), **process_env}


# ---------------------------------------------------------------- status


@dataclass(frozen=True)
class Status:
    """The answers of ``ccp-capture status``.

    Attributes:
        installed: All six hooks and the address and token are in the settings file.
        missing_events: The events without a capture hook.
        reachable: The API answered ``/health`` (even "degraded"); ``None`` if no address is known.
        token_valid: True/False if the API accepted/refused the token; ``None`` if it could not
            be asked (API unreachable, or an older API without the probe's endpoint).
        pending_uploads: Waiting lifecycle events plus transcripts with bytes not uploaded yet.
        last_upload: When the client last moved an upload position, or ``None``.
    """

    installed: bool
    missing_events: list[str]
    reachable: bool | None
    token_valid: bool | None
    pending_uploads: int
    last_upload: datetime | None


def collect_status(settings_path: Path, process_env: Mapping[str, str]) -> Status:
    """Gather everything ``status`` prints. Nothing here prints or raises for a bad file."""
    env = effective_env(settings_path, process_env)
    missing = [e for e in HOOK_TIMEOUTS if e not in installed_events(settings_path)]
    own = installed_env(settings_path)
    installed = not missing and ENV_API_URL in own and ENV_TOKEN in own
    api_url = env.get(ENV_API_URL, "").rstrip("/")
    token = env.get(ENV_TOKEN, "")
    reachable = _api_reachable(api_url) if api_url else None
    token_valid = _token_valid(api_url, token) if reachable and token else None
    state_dir = Path(env.get("CCP_STATE_DIR") or Path.home() / ".ccp" / "capture")
    return Status(
        installed=installed,
        missing_events=missing,
        reachable=reachable,
        token_valid=token_valid,
        pending_uploads=pending_uploads(state_dir),
        last_upload=last_upload_time(state_dir),
    )


def _api_reachable(api_url: str) -> bool:
    """True if the API answers ``GET /health`` at all (a 503 means it is up, but degraded)."""
    try:
        _open(urllib.request.Request(api_url + "/health"))  # noqa: S310
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return True


def _token_valid(api_url: str, token: str) -> bool | None:
    """Ask the API whether it accepts the token (see ``_TOKEN_PROBE_PATH``)."""
    request = urllib.request.Request(  # noqa: S310
        api_url + _TOKEN_PROBE_PATH,
        data=b"{}",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        _open(request)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return False
        return True if error.code in (400, 422) else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return True


def _open(request: urllib.request.Request) -> None:
    """Send a request with a short time limit and no redirects followed to other servers."""
    opener = urllib.request.build_opener()
    with opener.open(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
        response.read(1)


def pending_uploads(state_dir: Path) -> int:
    """Count what still waits to be uploaded, from the client's own state.

    That is every lifecycle event waiting for the API, plus every known transcript that has more
    bytes than the saved upload position of its main stream.
    """
    state = StateStore(state_dir)
    waiting = sum(
        len(state.marker_names(s, "lifecycle-")) for s in state.sessions_with_markers("lifecycle-")
    )
    for session_id, transcript in state.known_sessions():
        try:
            size = transcript.stat().st_size
        except OSError:
            continue
        if size > state.get_offset(session_id, MAIN_STREAM):
            waiting += 1
    return waiting


def last_upload_time(state_dir: Path) -> datetime | None:
    """Return when an upload position was last saved (the newest ``*.offset`` file), or None."""
    newest = 0.0
    try:
        for offset_file in state_dir.glob("*/*.offset"):
            newest = max(newest, offset_file.stat().st_mtime)
    except OSError:
        pass
    return datetime.fromtimestamp(newest).astimezone() if newest else None
