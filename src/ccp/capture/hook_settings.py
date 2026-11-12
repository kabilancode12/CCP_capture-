"""Build the Claude Code settings that register the capture hooks.

``ccp-capture hook-settings`` prints the result; you merge it into ``~/.claude/settings.json``
(or a project's ``.claude/settings.json``). Nothing is written automatically: those files belong
to the developer, and deploying the settings to a whole team is a later step (managed settings).

The token is **never** copied into the output, even if ``CCP_INGEST_TOKEN`` is set in the current
environment: the output contains a placeholder, and the real value is pasted in by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ccp.capture.hooks import HOOK_TIMEOUTS

TOKEN_PLACEHOLDER = "REPLACE_WITH_THE_INGEST_TOKEN"  # noqa: S105  (a placeholder, not a secret)


def hook_command() -> str:
    """Return the command that runs the hook handler with the current Python.

    Forward slashes are used so the path also works when the hook runs in Git Bash on Windows.
    """
    python = Path(sys.executable).as_posix()
    return f'"{python}" -m ccp.capture hook'


def build_hook_settings(command: str, api_url: str) -> dict[str, Any]:
    """Build the settings dictionary.

    Args:
        command: The shell command that runs the hook handler (see :func:`hook_command`).
        api_url: The ingestion API address to put in the ``env`` block.

    Returns:
        A dictionary with an ``env`` block (API address and a token placeholder) and one hook entry
        per event, each with its time limit.
    """
    hooks = {
        event: [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}]
        for event, timeout in HOOK_TIMEOUTS.items()
    }
    return {
        "env": {"CCP_API_URL": api_url, "CCP_INGEST_TOKEN": TOKEN_PLACEHOLDER},
        "hooks": hooks,
    }
