"""A small client for Metabase's web API, using only the standard library.

It sends JSON, signs in with a username and password (Metabase answers with a session id, which is
sent on every later request) and turns every failure into :class:`MetabaseError`. The transport is a
parameter so that the steps in ``setup.py`` can be tested without a running Metabase.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

TIMEOUT_SECONDS = 60

# (method, url, body or None, session id or None) -> (status code, decoded JSON or text)
Transport = Callable[[str, str, Any, str | None], tuple[int, Any]]


class MetabaseError(RuntimeError):
    """Raised when Metabase cannot be reached or refuses a request. Holds no password or token."""


def urllib_transport(method: str, url: str, body: Any, session: str | None) -> tuple[int, Any]:
    """Send one request with ``urllib`` and return the status and the decoded answer."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310  (http(s) URL)
    request.add_header("Content-Type", "application/json")
    if session:
        request.add_header("X-Metabase-Session", session)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(text)
        except json.JSONDecodeError:
            return error.code, text
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise MetabaseError(f"cannot reach Metabase at {url}: {type(error).__name__}") from error


class MetabaseClient:
    """Calls Metabase's API, remembering the session after :meth:`use_session`."""

    def __init__(self, base_url: str, transport: Transport = urllib_transport) -> None:
        """Remember the address (for example ``http://127.0.0.1:3000``) and how to send requests."""
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._session: str | None = None

    def use_session(self, session_id: str) -> None:
        """Send this session id with every later request."""
        self._session = session_id

    def login(self, email: str, password: str) -> None:
        """Sign in and keep the session. Raises :class:`MetabaseError` if the sign-in is refused."""
        answer = self.request("POST", "/api/session", {"username": email, "password": password})
        session = answer.get("id") if isinstance(answer, dict) else None
        if not isinstance(session, str):
            raise MetabaseError("sign-in answered without a session")
        self._session = session

    def request(self, method: str, path: str, body: Any = None) -> Any:
        """Send a request and return the decoded answer.

        Raises:
            MetabaseError: On any status of 400 or more, naming the method, path and status (and
                Metabase's own message, which holds no secret we sent).
        """
        status, answer = self._transport(method, self._base + path, body, self._session)
        if status >= 400:
            detail = answer if isinstance(answer, str) else json.dumps(answer)
            raise MetabaseError(f"{method} {path} answered {status}: {detail[:300]}")
        return answer
