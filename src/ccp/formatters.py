"""Small helpers that turn recorded values into text for the web pages."""

from __future__ import annotations


def format_session_name(session_number: int, title: str) -> str:
    """The display name of a session: ``Session 1: Add Login Endpoint``.

    Args:
        session_number: The session's number, counting from 1.
        title: The session's title; extra white space is made single.

    Returns:
        ``Session <number>: <title>``, or just ``Session <number>`` when the title is empty.
    """
    clean = " ".join(title.split())
    return f"Session {session_number}: {clean}" if clean else f"Session {session_number}"
