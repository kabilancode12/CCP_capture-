"""Helpers that show the *structure* of data without revealing its content.

Transcripts and local configuration can hold secrets and personal data. The inspectors
therefore never print raw values: they print the type, the length, or a short preview that
is safe to share in a review.
"""

from __future__ import annotations

from typing import Any

# Strings up to this length are shown as-is; longer ones are replaced by a length marker.
MAX_VISIBLE_LENGTH = 40


def mask_value(value: Any, max_visible: int = MAX_VISIBLE_LENGTH) -> str:
    """Describe a single value without exposing long or sensitive text.

    Args:
        value: Any JSON-compatible value.
        max_visible: Longest string that may be shown unchanged.

    Returns:
        A short description: the string itself if short, ``<str len=N>`` if long,
        or the type name for non-string values.
    """
    if isinstance(value, str):
        return value if len(value) <= max_visible else f"<str len={len(value)}>"
    if value is None:
        return "null"
    return f"<{type(value).__name__}>"


def shape_of(value: Any, depth: int = 3, max_visible: int = MAX_VISIBLE_LENGTH) -> Any:
    """Return the nested structure of a JSON value with every leaf masked.

    Dictionaries keep their keys, lists are summarised by length and the shape of their first
    item, and leaves are masked with :func:`mask_value`. Use it to show what a payload
    looks like without leaking what it contains.

    Args:
        value: Any JSON-compatible value.
        depth: How many levels to expand before collapsing to a type name.
        max_visible: Longest string shown unchanged. Pass ``0`` to hide every string
            (for example for account details such as an email address).

    Returns:
        A structure of the same layout as ``value`` but containing only masked leaves.
    """
    if depth <= 0:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {key: shape_of(item, depth - 1, max_visible) for key, item in value.items()}
    if isinstance(value, list):
        if not value:
            return []
        return [f"<list len={len(value)}>", shape_of(value[0], depth - 1, max_visible)]
    return mask_value(value, max_visible)
