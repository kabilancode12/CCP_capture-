"""Which files a shell command writes, moves or deletes (no database, no model call).

Claude Code's Edit and Write tools are recorded exactly (``file_change``). When Claude changes files
with a shell command instead, the command is the only record. This module reads the plain, clear
cases: ``> file``, ``>> file``, ``tee file``, ``sed -i ... file``, ``rm``, ``mv``, ``cp``,
``touch``, ``mkdir`` and project scaffolds such as ``dotnet new``. It never guesses: a command
it cannot read gives no files. Heredoc bodies and anything in ``/tmp`` or ``/dev`` are ignored.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

CREATED = "created"
MODIFIED = "modified"
DELETED = "deleted"

# `cat > f <<'EOF' ... EOF`: the body is text, not commands.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", re.DOTALL)
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|")
_REDIRECT = re.compile(r"(?<![0-9&<>])(>>?)\s*(?!&)(\S+)")

# Commands that create a whole project; their files are not named in the command.
_SCAFFOLDS = (
    (re.compile(r"\bdotnet\s+new\s+(\S+)"), "dotnet new {}"),
    (re.compile(r"\bnpm\s+(?:init|create)\b\s*(\S*)"), "npm init {}"),
    (re.compile(r"\bcargo\s+(?:new|init)\b\s*(\S*)"), "cargo new {}"),
    (re.compile(r"\bgo\s+mod\s+init\b"), "go mod init"),
)


@dataclass(frozen=True)
class ShellFile:
    """One file (or folder) a command changes."""

    path: str
    change: str  # created | modified | deleted


def files_changed_by(command: str) -> list[ShellFile]:
    """The files a command changes, in order, each path once (the last change wins).

    Args:
        command: The text of a Bash command.
    """
    text = _HEREDOC.sub(" ", command)
    found: dict[str, str] = {}
    for segment in _SEGMENT_SPLIT.split(text):
        for item in _segment(segment.strip()):
            if _wanted(item.path):
                found[item.path] = item.change
    return [ShellFile(path, change) for path, change in found.items()]


def _segment(segment: str) -> list[ShellFile]:
    if not segment:
        return []
    items = [
        ShellFile(_clean(match.group(2)), MODIFIED if match.group(1) == ">>" else CREATED)
        for match in _REDIRECT.finditer(segment)
    ]
    for pattern, label in _SCAFFOLDS:
        match = pattern.search(segment)
        if match:
            name = match.group(1) if match.groups() else ""
            items.append(ShellFile(f"(project files from {label.format(name).strip()})", CREATED))
    try:
        words = shlex.split(segment, comments=False)
    except ValueError:
        return items
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words = words[1:]  # leading VAR=value
    if not words:
        return items
    program, rest = words[0].rsplit("/", 1)[-1], words[1:]
    targets = [word for word in rest if not word.startswith("-") and not word.startswith(">")]
    if program == "tee":
        items += [ShellFile(_clean(word), CREATED) for word in targets]
    elif program == "sed" and any(w == "-i" or w.startswith("-i") for w in rest) and targets:
        items.append(ShellFile(_clean(targets[-1]), MODIFIED))  # the file is the last argument
    elif program == "rm":
        items += [ShellFile(_clean(word), DELETED) for word in targets]
    elif program in ("touch", "mkdir"):
        items += [ShellFile(_clean(word), CREATED) for word in targets]
    elif program in ("mv", "cp") and len(targets) >= 2:
        if program == "mv":
            items += [ShellFile(_clean(word), DELETED) for word in targets[:-1]]
        items.append(ShellFile(_clean(targets[-1]), CREATED))
    return items


def _clean(path: str) -> str:
    return path.strip().strip("'\"")


def _wanted(path: str) -> bool:
    """Skip empty paths, variables, devices and scratch files outside the project."""
    if not path or "$" in path or "`" in path or "*" in path:
        return False
    return not path.startswith(("/dev/", "/tmp/", "/proc/"))  # noqa: S108 (a filter, not a file)
