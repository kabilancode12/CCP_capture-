"""The capture client's connection to the ingestion API.

Built on ``urllib`` from the standard library, so a developer's machine needs nothing installed.

Errors are split by what the caller should do:

* :class:`ApiUnavailableError`: the server could not be reached or had a problem (network down,
  timeout, 5xx, 429), or the answer was not the API's own (for example a proxy or a captive portal
  page instead of JSON). Nothing is wrong with the request: try again later.
* :class:`ApiRejectedError`: the server refused the request (a 4xx such as 401 or 422). Repeating
  the same request will not help.

Neither error message contains the token or any transcript content.

Fail fast: a client made with ``fail_fast=True`` (the hooks and the flush use it) stops trying once
a request could not reach the server at all (no connection, or a timeout). Every later request of
that client fails at once with :class:`ApiUnavailableError`, instead of waiting for its own timeout:
one unreachable API costs a hook one timeout, not one per request. A server that answered (even with
an error) does not trigger this.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ccp.environment import EnvironmentRecord
from ccp.lifecycle import LifecycleEvent
from ccp.redaction.records import RedactionRecord
from ccp.settings import Secret


class ApiError(RuntimeError):
    """Base class of API errors."""


class ApiUnavailableError(ApiError):
    """The API could not be reached or failed on its side; retrying later may work."""


class ApiRejectedError(ApiError):
    """The API refused the request; retrying the same request will not help.

    Attributes:
        status: The HTTP status code.
    """

    def __init__(self, status: int, message: str) -> None:
        """Remember the HTTP ``status`` and the server's ``message``."""
        super().__init__(f"rejected ({status}): {message}")
        self.status = status


@dataclass(frozen=True)
class ToolResultReply:
    """The API's answer to a tool-result upload.

    Attributes:
        stored: True if anything was written (the file is new, or a changed version of it).
        key: The object key that holds this content (the file's key, or the key of a changed
            version, ``<key>@<hash>``), or ``None`` if the answer did not say.
    """

    stored: bool
    key: str | None = None


class ApiPort(Protocol):
    """What the capture client needs from the API (a fake can stand in for tests)."""

    def put_chunk(self, session_id: str, stream: str, offset: int, body: bytes) -> bool:
        """Upload a chunk; return True if it was stored, False if it was already there."""

    def put_attachment(self, digest: str, body: bytes) -> bool:
        """Upload an attachment body; return True if stored, False if already there."""

    def post_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        """Report where redaction rules fired."""

    def put_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        """Store or merge the session's environment record."""

    def post_lifecycle(self, session_id: str, event: LifecycleEvent) -> None:
        """Report that a session of the chat started or ended."""

    def post_snapshot(self, session_id: str, record: dict[str, Any]) -> None:
        """Report one snapshot commit of the capture repository."""

    def put_tool_result(self, session_id: str, name: str, body: bytes) -> ToolResultReply:
        """Upload a tool-result file (redacted); say whether it was stored, and under which key."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    urllib would resend the Authorization header to the redirect target, which could be another
    server. The API never redirects, so a redirect is treated as an error.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


class HttpApiClient:
    """:class:`ApiPort` over HTTP(S)."""

    def __init__(
        self,
        base_url: str,
        token: Secret,
        timeout_seconds: float = 15.0,
        *,
        fail_fast: bool = False,
    ) -> None:
        """Create a client.

        Args:
            base_url: The API address, for example ``http://127.0.0.1:8000``.
            token: The bearer token.
            timeout_seconds: Time limit for each request.
            fail_fast: Stop trying once the server could not be reached (see the module
                description). For short-lived processes such as a hook.
        """
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirects)
        self._fail_fast = fail_fast
        self._unreachable = False

    def put_chunk(self, session_id: str, stream: str, offset: int, body: bytes) -> bool:
        """Upload a chunk (``PUT .../chunks/{offset}``)."""
        path = f"/v1/sessions/{session_id}/streams/{stream}/chunks/{offset}"
        return _stored(self._send("PUT", path, body, "application/x-ndjson"))

    def put_attachment(self, digest: str, body: bytes) -> bool:
        """Upload an attachment body (``PUT /v1/attachments/{digest}``)."""
        return _stored(self._send("PUT", f"/v1/attachments/{digest}", body, "application/json"))

    def post_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        """Report redactions (``POST /v1/sessions/{id}/redactions``)."""
        payload = {
            "records": [
                {"rule_id": r.rule_id, "location": r.location, "occurrences": r.occurrences}
                for r in records
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        self._send("POST", f"/v1/sessions/{session_id}/redactions", body, "application/json")

    def put_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        """Send the known fields of the environment record (``PUT .../environment``)."""
        body = json.dumps(record.to_payload()).encode("utf-8")
        self._send("PUT", f"/v1/sessions/{session_id}/environment", body, "application/json")

    def post_lifecycle(self, session_id: str, event: LifecycleEvent) -> None:
        """Report a session start or end (``POST /v1/sessions/{id}/lifecycle``)."""
        body = json.dumps(event.to_payload()).encode("utf-8")
        self._send("POST", f"/v1/sessions/{session_id}/lifecycle", body, "application/json")

    def post_snapshot(self, session_id: str, record: dict[str, Any]) -> None:
        """Report a snapshot commit (``POST /v1/sessions/{id}/snapshots``)."""
        body = json.dumps(record).encode("utf-8")
        self._send("POST", f"/v1/sessions/{session_id}/snapshots", body, "application/json")

    def put_tool_result(self, session_id: str, name: str, body: bytes) -> ToolResultReply:
        """Upload a tool-result file (``PUT /v1/sessions/{id}/tool-results/{name}``)."""
        path = f"/v1/sessions/{session_id}/tool-results/{name}"
        reply = self._send("PUT", path, body, "text/plain; charset=utf-8")
        key = reply.get("key")
        return ToolResultReply(_stored(reply), key if isinstance(key, str) else None)

    def _send(self, method: str, path: str, body: bytes, content_type: str) -> dict[str, Any]:
        """Send a request and return the decoded JSON response.

        Returns:
            The decoded JSON object; an empty dictionary for an empty body or a JSON value that is
            not an object.

        Raises:
            ApiUnavailableError: On a network error, a timeout, a 5xx or 429 response, or a
                success response whose body is not JSON (not the API's own answer); at once,
                without trying, if the client fails fast and the server could not be reached before.
            ApiRejectedError: On any other error response.
        """
        if self._unreachable:
            raise ApiUnavailableError("cannot reach the API (not tried again in this run)")
        request = urllib.request.Request(  # noqa: S310  (scheme is validated in the settings)
            self._base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token.reveal()}",
                "Content-Type": content_type,
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            message = _server_message(error)
            if error.code >= 500 or error.code == 429:
                raise ApiUnavailableError(f"server error ({error.code}): {message}") from error
            raise ApiRejectedError(error.code, message) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            self._unreachable = self._fail_fast
            raise ApiUnavailableError(f"cannot reach the API: {type(error).__name__}") from error
        return _decode_reply(raw)


def _decode_reply(raw: bytes) -> dict[str, Any]:
    """Decode a success response body.

    Raises:
        ApiUnavailableError: If the body is not JSON. Something between the client and the API
            (a proxy, a captive portal) answered instead; the request may work later.
    """
    try:
        decoded = json.loads(raw or b"{}")
    except ValueError as error:  # includes JSONDecodeError and UnicodeDecodeError
        raise ApiUnavailableError("the answer is not JSON (not from the API?)") from error
    return decoded if isinstance(decoded, dict) else {}


def _stored(reply: dict[str, Any]) -> bool:
    """Return the ``stored`` flag of an upload reply.

    Raises:
        ApiUnavailableError: If the reply has no boolean ``stored`` field. The API always sends
            one, so the answer came from something else; treating it as success could lose data.
    """
    stored = reply.get("stored")
    if not isinstance(stored, bool):
        raise ApiUnavailableError("the answer has no 'stored' flag (not from the API?)")
    return stored


def _server_message(error: urllib.error.HTTPError) -> str:
    """Return the server's ``{"error": "..."}`` message, or a generic one."""
    try:
        decoded = json.loads(error.read())
        message = decoded.get("error") if isinstance(decoded, dict) else None
        return str(message) if message else "no message"
    except (ValueError, OSError):
        return "no message"
