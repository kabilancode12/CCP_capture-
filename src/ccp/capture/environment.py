"""Collect the environment record on the developer's machine.

Two stages, because not everything is known at the start of a session (the transcript file does
not exist yet at SessionStart):

* :func:`collect_process_environment`: what this process, the operating system, the Claude Code
  login file and Git (for the working folder) say. Available at SessionStart.
* :func:`collect_transcript_environment`: what Claude Code wrote into the transcript itself (its
  version and the shell it uses for tools). Available once the transcript exists.

The two records are merged (``EnvironmentRecord.merged_with``) and sent to the server, which merges
them again with anything it already has.

The detection rules for surface, shell and access path live in
:mod:`ccp.verify.environment_inspector`; they are guesses marked ``[verify]`` until each surface
has been tested.

Values are collected as they are (user name, host name, account e-mail, Git identity): this is the
purpose of the record, see :mod:`ccp.environment`. Two values are never sent as they are: Claude
Code's ``machineID`` (only the first 16 hex digits of its SHA-256 hash, ``machine_key``) and the Git
remote (its user name and password are removed). Nothing here reads or sends transcript content.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import platform
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccp.capture.gitinfo import GitFacts, read_git_facts
from ccp.environment import MAX_VALUE_LENGTH, EnvironmentRecord
from ccp.verify.environment_inspector import detect_access_path, detect_shell, detect_surface

# How many transcript lines are examined for the version and environment entries. They are among
# the first lines of a transcript.
MAX_TRANSCRIPT_LINES = 400

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class HostInfo:
    """Facts about the machine (a value object so tests can supply their own)."""

    os_name: str
    os_version: str
    architecture: str
    hostname: str
    user: str

    @classmethod
    def current(cls) -> HostInfo:
        """Read the facts from the machine this runs on."""
        try:
            user = getpass.getuser()
        except (OSError, KeyError):
            user = ""
        return cls(
            os_name=platform.system(),
            os_version=f"{platform.release()} {platform.version()}",
            architecture=platform.machine(),
            hostname=platform.node(),
            user=user,
        )


def _clean(value: object) -> str | None:
    """Return ``value`` as a short, single-line string, or ``None`` if it is empty or not text."""
    if not isinstance(value, str):
        return None
    text = _CONTROL_CHARACTERS.sub(" ", value).strip()
    return text[:MAX_VALUE_LENGTH] or None


def _read_claude_config(home: Path) -> dict[str, Any]:
    """Return the contents of ``~/.claude.json`` (empty if it is missing or not a JSON object)."""
    try:
        config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _account_of(config: dict[str, Any]) -> dict[str, Any]:
    """Return the logged-in account (``oauthAccount``; observed on Claude Code 2.1.325)."""
    account = config.get("oauthAccount")
    return account if isinstance(account, dict) else {}


def machine_key(machine_id: object) -> str | None:
    """Return the first 16 hex digits of the SHA-256 hash of Claude Code's ``machineID``.

    The raw id is never sent: the hash tells machines apart without revealing the id itself.
    """
    if not isinstance(machine_id, str) or not machine_id.strip():
        return None
    return hashlib.sha256(machine_id.strip().encode("utf-8")).hexdigest()[:16]


def collect_process_environment(
    env: Mapping[str, str],
    home: Path,
    host: HostInfo | None = None,
    cwd: Path | None = None,
    git_facts: Callable[[Path], GitFacts] = read_git_facts,
) -> EnvironmentRecord:
    """Collect what is known at SessionStart.

    Args:
        env: Environment variables of the hook process (``CLAUDE_CODE_ENTRYPOINT`` is only set in
            processes that Claude Code starts, so this must run inside a hook).
        home: The user's home directory (where ``.claude.json`` is).
        host: Facts about the machine; defaults to the machine this runs on.
        cwd: The chat's working folder (the hook payload's ``cwd``); ``None`` if unknown, and then
            Git is not asked.
        git_facts: Reads the Git facts of a folder (replaced in tests).
    """
    host = host or HostInfo.current()
    config = _read_claude_config(home)
    account = _account_of(config)
    git = git_facts(cwd) if cwd is not None else GitFacts()
    if env.get("ANTHROPIC_API_KEY"):  # only whether a key is set; never the key
        auth_type: str | None = "api_key"
    else:
        auth_type = "subscription" if account else None
    return EnvironmentRecord(
        entrypoint=_clean(env.get("CLAUDE_CODE_ENTRYPOINT")),
        surface=_clean(detect_surface(env)["value"]),
        hook_shell=_clean(detect_shell(env)["value"]),
        access_path=_clean(detect_access_path(env, Path("/.dockerenv"))["value"]),
        os_name=_clean(host.os_name),
        os_version=_clean(host.os_version),
        cpu_architecture=_clean(host.architecture),
        os_user=_clean(host.user),
        hostname=_clean(host.hostname),
        account_name=_clean(account.get("displayName") or account.get("fullName")),
        account_email=_clean(account.get("emailAddress")),
        organisation_name=_clean(account.get("organizationName")),
        organisation_uuid=_clean(account.get("organizationUuid")),
        auth_type=auth_type,
        account_uuid=_clean(account.get("accountUuid")),
        full_name=_clean(account.get("fullName")),
        organisation_role=_clean(account.get("organizationRole")),
        machine_key=machine_key(config.get("machineID")),
        folder=_clean(str(cwd)) if cwd is not None else None,
        git_remote=_clean(git.remote),
        git_user_name=_clean(git.user_name),
        git_user_email=_clean(git.user_email),
    )


def collect_transcript_environment(transcript: Path) -> EnvironmentRecord:
    """Collect what Claude Code wrote about its own environment into the transcript.

    Looks for the ``version`` key of the entries and for the ``environment`` attachment (the shell
    Claude uses for tools, and the operating system version). Only these two things are read; no
    prompts or answers are looked at. [verify] Layout observed on Claude Code 2.1.289.

    Returns:
        A record with whatever was found; an empty record if the file is missing or unreadable.
    """
    version: str | None = None
    tool_shell: str | None = None
    os_version: str | None = None
    try:
        with transcript.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= MAX_TRANSCRIPT_LINES or (version and tool_shell):
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                version = version or _clean(entry.get("version"))
                snapshot = _environment_snapshot(entry)
                if snapshot:
                    tool_shell = tool_shell or _clean(snapshot.get("shell"))
                    os_version = os_version or _clean(snapshot.get("osVersion"))
    except OSError:
        return EnvironmentRecord()
    return EnvironmentRecord(
        claude_code_version=version, tool_shell=tool_shell, os_version=os_version
    )


def collect_environment(
    env: Mapping[str, str],
    home: Path,
    transcript: Path | None = None,
    host: HostInfo | None = None,
    cwd: Path | None = None,
) -> EnvironmentRecord:
    """Collect the whole environment record.

    That is the process stage, plus the transcript stage if the transcript exists yet.

    Args:
        env: Environment variables of the hook process.
        home: The user's home directory.
        transcript: The session transcript, or ``None`` if it does not exist yet.
        host: Facts about the machine; defaults to the machine this runs on.
        cwd: The chat's working folder, or ``None`` if unknown.
    """
    record = collect_process_environment(env, home, host, cwd)
    if transcript is not None:
        record = record.merged_with(collect_transcript_environment(transcript))
    return record


def _environment_snapshot(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``snapshot`` of an ``environment`` attachment entry, if this is one."""
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "environment":
        snapshot = attachment.get("snapshot")
        return snapshot if isinstance(snapshot, dict) else None
    return None
