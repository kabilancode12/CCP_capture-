"""Recognising test runs in shell commands.

A test run is a shell call (``Bash`` or ``PowerShell``) whose command starts a known test runner.
The transcript does not record an exit code for a success. It does record a failure: the tool result
is an error and its text starts with ``Exit code <n>``. So the outcome is read like this:

* no error: the command exited with 0, so the tests **passed**;
* an error with ``Exit code <n>``: they **failed**;
* interrupted, or an error without an exit code: unknown (``None``);
* a permission denial: the command never ran, so it is not a test run at all (the caller skips it).

A request to a service on this machine (``curl http://localhost:5000/...``) is recorded as a manual
check (``http check (manual)``), because that is how a check is often done when no test suite
exists; its outcome is the command's exit code, so a ``curl`` that gets an HTTP error still counts
as passed unless it was run with ``-f``.

Limits, deliberately simple: the outcome is the *command's* exit code, so a pipeline such as
``pytest | tail`` reports the exit code of ``tail``; the number of tests passed or failed is not
read; a command with several runners counts once, for the first.
"""

from __future__ import annotations

import re

# Wrappers that may come before the runner: "uv run pytest", "python -m pytest", "npx jest".
_WRAPPER = (
    r"(?:(?:uv|poetry|pipenv|pdm|hatch)\s+run\s+|npx\s+|"
    r"\S*python[\d.]*(?:\.exe)?\s+-m\s+|py\s+-m\s+)*"
)

# Runner name and the start of the command that runs it, in the order they are tried.
_RUNNERS: tuple[tuple[str, str], ...] = (
    ("pytest", r"(?:pytest|py\.test)\b"),
    ("unittest", r"unittest\b"),
    ("npm test", r"(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b"),
    ("jest", r"jest\b"),
    ("vitest", r"vitest\b"),
    ("dotnet test", r"dotnet\s+test\b"),
    ("go test", r"go\s+test\b"),
    ("cargo test", r"cargo\s+test\b"),
    ("maven", r"(?:mvn|\./mvnw|mvnw)\s+(?:\S+\s+)*test\b"),
    ("gradle", r"(?:gradle|\./gradlew|gradlew)\s+(?:\S+\s+)*test\b"),
    ("phpunit", r"(?:\./vendor/bin/)?phpunit\b"),
    ("rspec", r"(?:bundle\s+exec\s+)?rspec\b"),
)
_PATTERNS = tuple((name, re.compile(_WRAPPER + pattern)) for name, pattern in _RUNNERS)

# A manual check of a service running on this machine: an HTTP client aimed at a local address.
# Claude often verifies an API this way instead of running a test suite; it counts as a (manual)
# test run. A download (``curl -O https://...``) does not: only local addresses count.
_HTTP_CLIENT = re.compile(
    r"(?:curl|wget|http|https|xh|Invoke-WebRequest|Invoke-RestMethod|iwr|irm)\b"
)
_LOCAL_ADDRESS = re.compile(
    r"(?:localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|\[::1\]|\b::1\b)(?::\d+|/|\b)", re.IGNORECASE
)
MANUAL_CHECK = "http check (manual)"

# Commands that start a runner without running tests.
_NOT_A_RUN = re.compile(r"(?:^|\s)(?:--collect-only|--co|--help|-h|--version)(?:\s|$)")

# A command is made of segments joined by these; the runner is at the start of a segment.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
# An environment assignment before the command, such as "CI=1 pytest".
_ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")

# A here-document body (``cat > f <<'EOF' ... EOF``): file content, not commands.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", re.DOTALL)

_EXIT_CODE = re.compile(r"^Exit code (\d+)")


def detect_test_runner(command: str) -> str | None:
    """Name the test runner a shell command starts, or None if it does not run tests.

    Only the *start* of each part of the command counts: ``pytest -q`` and
    ``cd app && python -m pytest`` are test runs, ``pip install pytest`` and
    ``git commit -m "fix pytest"`` are not.

    Args:
        command: The command line as given to the shell tool.
    """
    command = _HEREDOC.sub(" ", command)  # text written into a file is not a command that runs
    for segment in _SEGMENT_SPLIT.split(command):
        text = _ENV_PREFIX.sub("", segment.strip())
        if _NOT_A_RUN.search(text):
            continue
        for name, pattern in _PATTERNS:
            if pattern.match(text):
                return name
        if _HTTP_CLIENT.match(text) and _LOCAL_ADDRESS.search(text):
            return MANUAL_CHECK
    return None


def exit_code_of(result_text: str | None) -> int | None:
    """The exit code in a failed tool result's text (``Exit code 1 ...``), or None."""
    match = _EXIT_CODE.match(result_text or "")
    return int(match.group(1)) if match else None


def outcome(
    *, is_error: bool, exit_code: int | None, interrupted: bool
) -> tuple[bool | None, int | None]:
    """Whether the tests passed, and the exit code, from how the tool call ended.

    Returns:
        ``(passed, exit_code)``. ``passed`` is None when it cannot be told.
    """
    if interrupted:
        return None, exit_code
    if not is_error:
        return True, 0
    if exit_code is None:
        return None, None
    return exit_code == 0, exit_code
