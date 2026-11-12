"""Run one hook: read the payload, do the action, log the outcome, never fail.

``ccp-capture hook`` calls :func:`run_hook`. Everything that can go wrong (a missing setting, an
unreachable API, a bug) is caught and written to the log file; the exit code is always 0 and
nothing is printed, because Claude Code treats a hook's output as part of the conversation and a
failing exit code as a reason to stop.

The log file is ``<state folder>/hook.log`` (rotated at 1 MB, three old files kept). It contains
event names, ids, counts and short reasons: never prompts, answers, file content, tokens or paths.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ccp.capture.api import HttpApiClient
from ccp.capture.client import CaptureClient
from ccp.capture.environment import collect_environment
from ccp.capture.hooks import HookHandler, parse_payload
from ccp.capture.process import spawn_detached
from ccp.capture.state import StateStore
from ccp.settings import ClientSettings, SettingsError, load_client_settings

LOG_FILE_NAME = "hook.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3

logger = logging.getLogger("ccp.capture.hook")


def build_handler(settings: ClientSettings, env: Mapping[str, str], home: Path) -> HookHandler:
    """Create the hook handler with a real client, state folder and process launcher.

    Args:
        settings: The client settings.
        env: Environment variables (used to detect the surface and the account).
        home: The user's home directory.
    """
    state = StateStore(settings.state_dir)
    client = CaptureClient(
        HttpApiClient(settings.api_url, settings.token, settings.timeout_seconds),
        state,
        chunk_bytes=settings.chunk_bytes,
        budget_seconds=settings.budget_seconds,
    )

    def start_flush(session_id: str, transcript: Path) -> None:
        spawn_detached(
            [
                sys.executable,
                "-m",
                "ccp.capture",
                "sync",
                "--session-id",
                session_id,
                str(transcript),
            ]
        )

    return HookHandler(
        client,
        state,
        lambda transcript: collect_environment(env, home, transcript),
        start_flush,
    )


def run_hook(stdin_text: str, env: Mapping[str, str], home: Path) -> int:
    """Handle one hook call.

    Args:
        stdin_text: The hook payload (JSON) that Claude Code sent on standard input.
        env: Environment variables of the hook process.
        home: The user's home directory.

    Returns:
        Always 0.
    """
    default_log_folder = home / ".ccp" / "capture"
    handler: logging.Handler | None = None
    try:
        try:
            settings = load_client_settings(env)
            log_folder = settings.state_dir
        except SettingsError as error:  # the message names variables, never values
            configured = env.get("CCP_STATE_DIR")  # log where the developer expects it, if set
            handler = _attach_log(Path(configured) if configured else default_log_folder)
            logger.error("settings invalid: %s", str(error).replace("\n", " "))
            return 0
        handler = _attach_log(log_folder)

        payload = parse_payload(stdin_text)
        if payload is None:
            logger.warning("unusable hook payload (not a JSON object with an event and session id)")
            return 0

        started = time.monotonic()
        outcome = build_handler(settings, env, home).handle(payload)
        logger.info(
            "%s session=%s %s (%.1fs)",
            payload.event,
            payload.session_id,
            outcome,
            time.monotonic() - started,
        )
    except Exception:  # noqa: BLE001  (deliberate: a hook must never fail or block Claude Code)
        logger.exception("hook failed")
    finally:
        if handler is not None:
            logging.getLogger("ccp.capture").removeHandler(handler)
            handler.close()
    return 0


def _attach_log(folder: Path) -> logging.Handler | None:
    """Send this module's log lines to the rotating log file in ``folder``.

    Returns the handler (to be removed afterwards), or ``None`` if the file cannot be opened: a
    broken log must not stop the hook.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / LOG_FILE_NAME,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    # One handler on the parent logger ``ccp.capture``: it receives the hook's own lines
    # (``ccp.capture.hook``) and the client's lines (``ccp.capture``) exactly once each. Attaching
    # it to the child as well would write every hook line twice.
    parent = logging.getLogger("ccp.capture")
    parent.addHandler(handler)
    parent.setLevel(logging.INFO)
    return handler
