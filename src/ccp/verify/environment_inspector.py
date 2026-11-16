"""Describe the machine and surface Claude Code runs on.

Each session record should say who ran it, with which Claude Code, and where. This tool
shows what can be detected locally and how. It reports **where** account details are stored and
**which fields** exist, never the values themselves.

Every detection rule is a guess about Claude Code or the operating system and is marked
``[verify]``: run the tool on each surface (terminal, VS Code, JetBrains, ...) and compare.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ccp.verify.masking import shape_of

# Environment variables whose *value* is safe and useful to show. All others: presence only.
SAFE_VALUE_VARIABLES = ("TERM_PROGRAM", "TERMINAL_EMULATOR", "CLAUDE_CODE_ENTRYPOINT", "MSYSTEM")


def detect(value: str, evidence: str) -> dict[str, str]:
    """Build one detection result.

    Args:
        value: What was detected (for example ``"vscode"``).
        evidence: The rule or marker that led to it, so a reviewer can judge it.
    """
    return {"value": value, "evidence": evidence}


def detect_surface(env: Mapping[str, str]) -> dict[str, str]:
    """Guess where Claude Code runs: terminal, VS Code, JetBrains or the web ([verify]).

    ``CLAUDE_CODE_ENTRYPOINT`` is the most reliable marker. Observed on Claude Code 2.1.289:
    ``cli`` in a terminal and ``claude-vscode`` in the VS Code extension (the extension does
    not set ``TERM_PROGRAM``). Other values are not yet known.
    """
    entrypoint = env.get("CLAUDE_CODE_ENTRYPOINT", "")
    if entrypoint == "claude-vscode":
        return detect("vscode", "CLAUDE_CODE_ENTRYPOINT=claude-vscode")
    if entrypoint == "cli":
        return detect("terminal", "CLAUDE_CODE_ENTRYPOINT=cli")
    if "desktop" in entrypoint:  # the Claude desktop app ([verify] the exact value)
        return detect("desktop", f"CLAUDE_CODE_ENTRYPOINT={entrypoint}")
    if entrypoint.startswith("sdk"):  # claude -p and the Agent SDK
        return detect("sdk", f"CLAUDE_CODE_ENTRYPOINT={entrypoint}")
    if "jetbrains" in entrypoint or "intellij" in entrypoint:
        return detect("jetbrains", f"CLAUDE_CODE_ENTRYPOINT={entrypoint}")
    if env.get("CLAUDE_CODE_REMOTE"):
        return detect("web-or-remote", "CLAUDE_CODE_REMOTE is set")
    if env.get("TERM_PROGRAM") == "vscode":
        return detect("vscode", "TERM_PROGRAM=vscode")
    if env.get("TERMINAL_EMULATOR", "").startswith("JetBrains"):
        return detect("jetbrains", "TERMINAL_EMULATOR starts with JetBrains")
    if entrypoint:  # a value not known yet: keep it rather than guess "terminal"
        return detect(entrypoint, f"CLAUDE_CODE_ENTRYPOINT={entrypoint} (not known yet)")
    return detect("terminal", "no IDE or remote marker found")


def detect_access_path(env: Mapping[str, str], container_marker: Path) -> dict[str, str]:
    """Guess how the user reached this machine: local, SSH, container or remote.

    [verify] All markers below are unconfirmed.

    Args:
        env: Environment variables.
        container_marker: File whose existence indicates a Docker container
            (normally ``/.dockerenv``; a parameter so tests can control it).
    """
    if env.get("SSH_CONNECTION") or env.get("SSH_CLIENT"):
        return detect("ssh", "SSH_CONNECTION or SSH_CLIENT is set")
    if env.get("REMOTE_CONTAINERS") or container_marker.exists():
        return detect("container", "REMOTE_CONTAINERS is set or /.dockerenv exists")
    if env.get("CLAUDE_CODE_REMOTE"):
        return detect("remote-session", "CLAUDE_CODE_REMOTE is set")
    return detect("local", "no SSH, container or remote marker found")


def detect_shell(env: Mapping[str, str]) -> dict[str, str]:
    """Guess the shell ([verify]).

    On Windows, cmd and PowerShell cannot be told apart from environment variables alone
    (``PSModulePath`` is set system-wide). Telling them apart needs the parent process name.
    """
    if env.get("WSL_DISTRO_NAME"):
        return detect("wsl", "WSL_DISTRO_NAME is set")
    if env.get("MSYSTEM"):
        return detect("git-bash", "MSYSTEM is set")
    shell = env.get("SHELL", "")
    if shell:
        return detect(Path(shell).name, "basename of SHELL")
    if env.get("ComSpec"):
        return detect("windows-console (cmd or powershell)", "only ComSpec is set; see docstring")
    return detect("unknown", "no shell marker found")


def detect_run_mode(env: Mapping[str, str]) -> dict[str, str]:
    """Guess interactive, headless or SDK from ``CLAUDE_CODE_ENTRYPOINT`` ([verify])."""
    entrypoint = env.get("CLAUDE_CODE_ENTRYPOINT")
    if entrypoint:
        return detect(entrypoint, "value of CLAUDE_CODE_ENTRYPOINT")
    return detect("unknown", "CLAUDE_CODE_ENTRYPOINT is not set in this process")


def read_account_fields(home: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Report where login details are stored and which fields exist, with values hidden.

    [verify] Claude Code appears to keep the logged-in account under the ``oauthAccount`` key
    of ``~/.claude.json``. Subscription versus API-key login is guessed from whether
    ``ANTHROPIC_API_KEY`` is set (only its presence is reported, never the key).

    Args:
        home: The user's home directory.
        env: Environment variables.
    """
    config_path = home / ".claude.json"
    result: dict[str, Any] = {
        "config_file": str(config_path),
        "config_file_exists": config_path.exists(),
        "api_key_in_environment": bool(env.get("ANTHROPIC_API_KEY")),
        "oauth_account_fields": None,
    }
    if not config_path.exists():
        return result
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["error"] = "config file could not be read as JSON"
        return result
    account = config.get("oauthAccount") if isinstance(config, dict) else None
    if isinstance(account, dict):
        result["oauth_account_fields"] = shape_of(account, max_visible=0)  # hide every value
    return result


def claude_version() -> str:
    """Return the output of ``claude --version``, or a note if it cannot be run."""
    executable = shutil.which("claude")
    if executable is None:
        return "claude not found on PATH"
    try:
        completed = subprocess.run(  # noqa: S603  (fixed arguments, no user input)
            [executable, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "claude --version failed"
    return completed.stdout.strip() or "no output"


def collect(env: Mapping[str, str], home: Path, container_marker: Path) -> dict[str, Any]:
    """Gather every environment fact into one JSON-serialisable report.

    Args:
        env: Environment variables (pass ``os.environ`` in normal use).
        home: The user's home directory.
        container_marker: File that indicates a container (see :func:`detect_access_path`).
    """
    return {
        "claude_code_version": claude_version(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "architecture": platform.machine(),
        },
        "surface": detect_surface(env),
        "run_mode": detect_run_mode(env),
        "shell": detect_shell(env),
        "access_path": detect_access_path(env, container_marker),
        "account": read_account_fields(home, env),
        # Values are masked: only the length of the user and host names is shown.
        "machine_user": shape_of(env.get("USERNAME") or env.get("USER") or "", max_visible=0),
        "hostname": shape_of(platform.node(), max_visible=0),
        "safe_environment_values": {
            name: env[name] for name in SAFE_VALUE_VARIABLES if name in env
        },
        "untested_surfaces": "All rules are [verify]. Repeat on each surface in the test matrix.",
    }
