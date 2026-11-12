"""Checks on an uploaded attachment body.

Attachment bodies are stored by the SHA-256 hash of their content (see
:mod:`ccp.storage.attachments`). The server verifies that the hash in the URL really is the hash
of the uploaded bytes, so a client can neither store content under a wrong name nor overwrite
another attachment: the same key always holds the same bytes.
"""

from __future__ import annotations

import hashlib
import json


class AttachmentError(ValueError):
    """Raised when an uploaded attachment is not acceptable. The message is safe to show."""


def validate_attachment(digest: str, body: bytes) -> None:
    """Check an attachment body against the digest it was uploaded under.

    Args:
        digest: The SHA-256 hex digest from the URL.
        body: The uploaded bytes (compact JSON, UTF-8).

    Raises:
        AttachmentError: If the body is empty, not UTF-8, not JSON, or its SHA-256 is not
            ``digest``.
    """
    if not body:
        raise AttachmentError("attachment is empty")
    try:
        json.loads(body.decode("utf-8"))  # any JSON value is allowed: an object, a string, ...
    except UnicodeDecodeError as error:
        raise AttachmentError("attachment is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise AttachmentError("attachment is not valid JSON") from error
    if hashlib.sha256(body).hexdigest() != digest:
        raise AttachmentError("attachment does not match the digest in the URL")
