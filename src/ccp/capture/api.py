"""The capture client's connection to the ingestion API.

Built on ``urllib`` from the standard library, so a developer's machine needs nothing installed.

Errors are split by what the caller should do:

* :class:`ApiUnavailableError`: the server could not be reached or had a problem (network down,
  timeout, 5xx, 429). Nothing is wrong with the request: try again later.
* :class:`ApiRejectedError`: the server refused the request (a 4xx such as 401 or 422). Repeating
  the same request will not help.

Neither error message contains the token or any transcript content.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any, Protocol

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


class ApiPort(Protocol):
    """What the capture client needs from the API (a fake can stand in for tests)."""

    def put_chunk(self, session_id: str, stream: str, offset: int, body: bytes) -> bool:
        """Upload a chunk; return True if it was stored, False if it was already there."""

    def put_attachment(self, digest: str, body: bytes) -> bool:
        """Upload an attachment body; return True if stored, False if already there."""

    def post_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        """Report where redaction rules fired."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    urllib would resend the Authorization header to the redirect target, which could be another
    server. The API never redirects, so a redirect is treated as an error.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


class HttpApiClient:
    """:class:`ApiPort` over HTTP(S)."""

    def __init__(self, base_url: str, token: Secret, timeout_seconds: float = 15.0) -> None:
        """Create a client.

        Args:
            base_url: The API address, for example ``http://127.0.0.1:8000``.
            token: The bearer token.
            timeout_seconds: Time limit for each request.
        """
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirects)

    def put_chunk(self, session_id: str, stream: str, offset: int, body: bytes) -> bool:
        """Upload a chunk (``PUT .../chunks/{offset}``)."""
        path = f"/v1/sessions/{session_id}/streams/{stream}/chunks/{offset}"
        return self._send("PUT", path, body, "application/x-ndjson")["stored"] is True

    def put_attachment(self, digest: str, body: bytes) -> bool:
        """Upload an attachment body (``PUT /v1/attachments/{digest}``)."""
        return (
            self._send("PUT", f"/v1/attachments/{digest}", body, "application/json")["stored"]
            is True
        )

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

    def _send(self, method: str, path: str, body: bytes, content_type: str) -> dict[str, Any]:
        """Send a request and return the decoded JSON response.

        Raises:
            ApiUnavailableError: On a network error, a timeout, or a 5xx or 429 response.
            ApiRejectedError: On any other error response.
        """
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
                decoded = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            message = _server_message(error)
            if error.code >= 500 or error.code == 429:
                raise ApiUnavailableError(f"server error ({error.code}): {message}") from error
            raise ApiRejectedError(error.code, message) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            raise ApiUnavailableError(f"cannot reach the API: {type(error).__name__}") from error
        return decoded if isinstance(decoded, dict) else {}


def _server_message(error: urllib.error.HTTPError) -> str:
    """Return the server's ``{"error": "..."}`` message, or a generic one."""
    try:
        decoded = json.loads(error.read())
        message = decoded.get("error") if isinstance(decoded, dict) else None
        return str(message) if message else "no message"
    except (ValueError, OSError):
        return "no message"
