"""Checksums used to cut false positives when looking for identity numbers.

An Aadhaar number carries a Verhoeff check digit. A random 12-digit number passes the check
only about one time in ten, so validating it keeps ordinary numbers (ids, counters) from being
redacted as personal data.
"""

from __future__ import annotations

# Standard Verhoeff tables. `_MULTIPLY` is dihedral-group multiplication, `_PERMUTE` the
# position-dependent permutation, `_INVERSE` the inverse used to compute a check digit.
_MULTIPLY = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_PERMUTE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INVERSE = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def _checksum(digits: str, offset: int) -> int:
    """Run the Verhoeff algorithm over ``digits`` (rightmost digit first)."""
    state = 0
    for position, char in enumerate(reversed(digits)):
        state = _MULTIPLY[state][_PERMUTE[(position + offset) % 8][int(char)]]
    return state


def is_valid_verhoeff(number: str) -> bool:
    """Return True if ``number`` (digits only, check digit last) passes the Verhoeff check."""
    return number.isdigit() and _checksum(number, offset=0) == 0


def verhoeff_check_digit(payload: str) -> str:
    """Return the Verhoeff check digit to append to ``payload`` (digits only).

    Used to build valid test numbers; ``is_valid_verhoeff(payload + check_digit)`` is True.
    """
    return str(_INVERSE[_checksum(payload, offset=1)])
