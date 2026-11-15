"""Passwords, signed session cookies and form tokens for the explorer website.

Standard library only:

* Passwords are hashed with scrypt (``hashlib.scrypt``) and a random salt. The stored text names
  the parameters, so they can be raised later without breaking old hashes.
* The session cookie is a small JSON object, base64-encoded and signed with HMAC-SHA256 under the
  site's secret (``CCP_EXPLORER_SECRET``). It is signed, not encrypted: it holds only a random
  session id, the user id, a password fingerprint and an expiry time, never anything secret.
* Every form that changes something carries a CSRF token: an HMAC of the session id, so a page on
  another site cannot submit a form in the user's name (it cannot read the token).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

# scrypt cost: 2**14 rounds of 8-block mixing need 16 MB of memory and about 50 ms per hash.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32

# Passwords shorter than this are refused (the users are people with access to audit data).
MIN_PASSWORD_LENGTH = 12

# A secret shorter than this is refused: the cookie signature is only as strong as the secret.
MIN_SECRET_LENGTH = 16


class PasswordError(ValueError):
    """Raised for a password that cannot be used (too short)."""


def hash_password(password: str) -> str:
    """Hash a password for storage in ``ui_user.password_hash``.

    Returns:
        ``scrypt$<n>$<r>$<p>$<salt>$<hash>``, with salt and hash in base64.

    Raises:
        PasswordError: If the password is shorter than :data:`MIN_PASSWORD_LENGTH`.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"A password needs at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "$".join(
        ["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt), _b64(digest)]
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Check a password against a stored hash, in constant time.

    A missing or unreadable hash never matches. When there is no hash, a dummy hash is still
    computed, so that the answer takes as long for an unknown user as for a wrong password.
    """
    parts = (stored or "").split("$")
    try:
        if len(parts) != 6 or parts[0] != "scrypt":
            raise ValueError("not an scrypt hash")
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        _scrypt(password, b"0" * _SALT_BYTES, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        return False
    try:
        actual = _scrypt(password, salt, n, r, p, len(expected))
    except (ValueError, MemoryError):  # parameters scrypt refuses: treat as unreadable
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class SessionData:
    """What the session cookie says.

    Attributes:
        session_id: Random, made at the first visit and again at each login.
        user_id: The logged-in user, or ``None`` before login.
        password_mark: A fingerprint of the user's password hash at login: a password reset ends
            the sessions that were opened with the old password.
        expires_at: Unix time after which the cookie is no longer accepted.
    """

    session_id: str
    user_id: int | None
    password_mark: str
    expires_at: int


class SessionSigner:
    """Signs and checks session cookies and form tokens with the site's secret."""

    def __init__(self, secret: str) -> None:
        """Derive separate keys for cookies, form tokens and password marks from ``secret``.

        Raises:
            ValueError: If the secret is shorter than :data:`MIN_SECRET_LENGTH`.
        """
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"The explorer secret (CCP_EXPLORER_SECRET) needs at least {MIN_SECRET_LENGTH} "
                "characters"
            )
        root = secret.encode()
        self._cookie_key = hmac.digest(root, b"ccp-explorer-cookie", "sha256")
        self._csrf_key = hmac.digest(root, b"ccp-explorer-csrf", "sha256")
        self._mark_key = hmac.digest(root, b"ccp-explorer-password-mark", "sha256")

    def new_session(self, user_id: int | None, password_mark: str, seconds: int) -> SessionData:
        """A fresh session (new random id) that lasts ``seconds``."""
        return SessionData(
            session_id=secrets.token_urlsafe(18),
            user_id=user_id,
            password_mark=password_mark,
            expires_at=int(time.time()) + seconds,
        )

    def sign(self, session: SessionData) -> str:
        """The cookie value for ``session``."""
        body = {
            "sid": session.session_id,
            "uid": session.user_id,
            "pm": session.password_mark,
            "exp": session.expires_at,
        }
        payload = _b64(json.dumps(body, separators=(",", ":")).encode())
        return f"{payload}.{self._signature(payload)}"

    def unsign(self, cookie: str | None) -> SessionData | None:
        """The session in a cookie, or ``None`` if it is missing, forged, malformed or expired."""
        if not cookie or "." not in cookie:
            return None
        payload, _, signature = cookie.partition(".")
        if not hmac.compare_digest(signature, self._signature(payload)):
            return None
        try:
            body: Any = json.loads(_unb64(payload))
            session = SessionData(
                session_id=str(body["sid"]),
                user_id=None if body["uid"] is None else int(body["uid"]),
                password_mark=str(body["pm"]),
                expires_at=int(body["exp"]),
            )
        except (ValueError, KeyError, TypeError, binascii.Error):
            return None
        return session if session.expires_at > time.time() else None

    def csrf_token(self, session: SessionData) -> str:
        """The form token that belongs to this session."""
        digest = hmac.digest(self._csrf_key, session.session_id.encode(), "sha256")
        return _b64(digest)

    def csrf_ok(self, session: SessionData | None, token: str) -> bool:
        """Whether ``token`` is this session's form token (constant-time comparison)."""
        return session is not None and hmac.compare_digest(token, self.csrf_token(session))

    def password_mark(self, password_hash: str | None) -> str:
        """A short fingerprint of a password hash (changes when the password changes)."""
        digest = hmac.digest(self._mark_key, (password_hash or "").encode(), "sha256")
        return _b64(digest)[:16]

    def _signature(self, payload: str) -> str:
        return _b64(hmac.digest(self._cookie_key, payload.encode(), "sha256"))


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, length: int = _HASH_BYTES) -> bytes:
    """Run scrypt with enough memory allowed for the chosen parameters."""
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=256 * r * n + 1024**2
    )


def _b64(data: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    """Decode :func:`_b64` (raises ``binascii.Error`` or ``ValueError`` on bad input)."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
