"""The model that reads a chat digest: the ``claude`` command, or the Anthropic API.

Every provider answers one question, :meth:`Provider.complete`: given a system prompt, a prompt
and a model name, return the model's text. Two are built in:

* :class:`ClaudeCliProvider` runs ``claude -p --output-format json --model <model>`` with the prompt
  on standard input and reads the ``result`` field of the JSON it prints. It runs with
  ``CCP_CAPTURE_DISABLED=1``, so the capture hooks do not record the extraction as a chat of its
  own, in an empty temporary folder (no project instructions are picked up), with no tools
  and without saving the session.
* :class:`AnthropicProvider` calls the Messages API with adaptive thinking. It is used only when the
  ``anthropic`` package is installed (it is not a dependency of this project) and
  ``ANTHROPIC_API_KEY`` is set; see :func:`choose_provider`.

Tests use a fake provider: no test calls a real model.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import subprocess  # noqa: S404  (runs the fixed `claude` command, never a shell)
import tempfile
from collections.abc import Mapping
from typing import Any, Protocol

from ccp.decisions.settings import DecisionSettings

# The capture hooks do nothing at all when this is set (ccp.capture.hook_runner).
CAPTURE_DISABLED = {"CCP_CAPTURE_DISABLED": "1"}

# Room for the longest answer: the decisions of a long chat as JSON.
API_MAX_TOKENS = 64_000

# How much of the command's error output an error message quotes.
_STDERR_TAIL = 300


class ProviderError(RuntimeError):
    """The model could not be asked, or its answer could not be read. Safe to show."""


class Provider(Protocol):
    """Something that returns a model's answer to a prompt."""

    def complete(self, system: str, prompt: str, model: str) -> str:
        """Return the model's text answer.

        Raises:
            ProviderError: The call failed, timed out or returned no text.
        """
        ...  # pragma: no cover


class ClaudeCliProvider:
    """Ask the model through the ``claude`` command in print mode."""

    def __init__(self, timeout_seconds: int, command: str = "claude") -> None:
        """Remember the time limit and the command to run (looked up on the PATH)."""
        self._timeout = timeout_seconds
        self._command = command

    def arguments(self, system: str, model: str) -> list[str]:
        """The command line (the prompt itself goes to standard input)."""
        executable = shutil.which(self._command)
        if executable is None:
            raise ProviderError(f"the {self._command!r} command was not found on the PATH")
        return [
            executable,
            "-p",
            "--output-format",
            "json",
            "--model",
            model,
            "--system-prompt",
            system,
            "--tools",
            "",  # no tools: the answer must come from the prompt alone
            "--no-session-persistence",
        ]

    def complete(self, system: str, prompt: str, model: str) -> str:
        """Run the command and return the ``result`` of its JSON output."""
        arguments = self.arguments(system, model)
        with tempfile.TemporaryDirectory(prefix="ccp-decisions-") as folder:
            try:
                finished = subprocess.run(  # noqa: S603  (fixed arguments, no shell)
                    arguments,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self._timeout,
                    cwd=folder,
                    env={**os.environ, **CAPTURE_DISABLED},
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise ProviderError(f"claude did not answer within {self._timeout} s") from error
            except OSError as error:
                raise ProviderError(f"claude could not be started: {error.strerror}") from error
        if finished.returncode != 0 and not finished.stdout.strip():
            tail = finished.stderr.strip()[-_STDERR_TAIL:]
            raise ProviderError(f"claude exited with code {finished.returncode}: {tail}")
        return parse_cli_output(finished.stdout)


def parse_cli_output(stdout: str) -> str:
    """Return the ``result`` text of ``claude -p --output-format json`` output.

    Raises:
        ProviderError: The output is not that JSON object, reports an error, or has no text.
    """
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ProviderError("claude did not print a JSON result") from error
    if not isinstance(output, dict):
        raise ProviderError("claude did not print a JSON result")
    if output.get("is_error") is True or output.get("subtype", "success") != "success":
        raise ProviderError(f"claude reported an error ({output.get('subtype', 'unknown')})")
    result = output.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ProviderError("claude returned no text")
    return result


class AnthropicProvider:
    """Ask the model through the Anthropic Messages API (needs the ``anthropic`` package)."""

    def __init__(self, timeout_seconds: int) -> None:
        """Create the client; it reads ``ANTHROPIC_API_KEY`` from the environment."""
        anthropic: Any = importlib.import_module("anthropic")  # imported only when used
        self._client: Any = anthropic.Anthropic(timeout=float(timeout_seconds))
        self._errors: tuple[type[BaseException], ...] = (anthropic.APIError,)

    def complete(self, system: str, prompt: str, model: str) -> str:
        """Stream one message (long answers would hit the HTTP timeout otherwise)."""
        try:
            with self._client.messages.stream(
                model=model,
                max_tokens=API_MAX_TOKENS,
                system=system,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
        except self._errors as error:
            raise ProviderError(f"the Anthropic API failed: {type(error).__name__}") from error
        if message.stop_reason == "refusal":
            raise ProviderError("the model declined to answer")
        if message.stop_reason == "max_tokens":
            raise ProviderError("the answer was cut off at the token limit")
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise ProviderError("the model returned no text")
        return text


def choose_provider(
    settings: DecisionSettings, env: Mapping[str, str] | None = None
) -> ClaudeCliProvider | AnthropicProvider:
    """The API when the ``anthropic`` package is installed and a key is set; else ``claude``."""
    variables = os.environ if env is None else env
    if variables.get("ANTHROPIC_API_KEY") and importlib.util.find_spec("anthropic") is not None:
        return AnthropicProvider(settings.timeout_seconds)
    return ClaudeCliProvider(settings.timeout_seconds)
