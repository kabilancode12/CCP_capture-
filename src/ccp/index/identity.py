"""Who a chat belongs to and what it is called (columns of ``session``, migration 016).

At every refresh the indexer works out, from the stored events:

* **account**: the account uuid the capture client sent with the environment record comes first;
  without one, the account uuid of the transcript's ``session-owner`` lines (the latest one). The
  account and the project are then linked by :func:`ccp.db.hierarchy.link_chat`, which knows the
  rules (project from the Git remote, else the folder, else the first main-transcript ``cwd``).
* **title**: the latest ``ai-title`` line of the main transcript (``aiTitle``).
* **is_empty**: the chat has no message: nothing the developer typed and nothing Claude answered
  (``prompt``, ``response``, ``tool_call`` or ``thinking`` in the main transcript). A chat that was
  opened and closed, or only ran a slash command, is empty. Dashboards and the catalog leave empty
  chats out.

The Git user name and e-mail come with the environment record and are copied by ``link_chat``.
Nothing here is ever deleted: accounts and projects are shared by many chats and carry labels.
"""

from __future__ import annotations

from typing import Any

from ccp.db.hierarchy import link_chat

# The newest session-owner line that names an account.
_OWNER = """
    SELECT payload ->> 'accountUuid', payload ->> 'organizationUuid' FROM event
     WHERE session_id = %s AND event_type = 'session-owner'
       AND jsonb_typeof(payload -> 'accountUuid') = 'string' AND payload ->> 'accountUuid' <> ''
     ORDER BY raw_object_key DESC, raw_line_number DESC LIMIT 1
"""

_TITLE = """
    SELECT NULLIF(btrim(payload ->> 'aiTitle'), '') FROM event
     WHERE session_id = %s AND event_type = 'ai-title'
       AND split_part(raw_object_key, '/', 3) = 'main'
       AND jsonb_typeof(payload -> 'aiTitle') = 'string' AND btrim(payload ->> 'aiTitle') <> ''
     ORDER BY raw_object_key DESC, raw_line_number DESC LIMIT 1
"""

# Whether the main transcript has any event at all, and whether one of them is a message.
_MESSAGES = """
    SELECT count(*) > 0,
           COALESCE(bool_or(event_type IN ('prompt', 'response', 'tool_call', 'thinking')), FALSE)
      FROM event
     WHERE session_id = %s AND split_part(raw_object_key, '/', 3) = 'main'
"""

# A chat whose transcript has not arrived has no events: nothing is known, so is_empty stays.
_SET_FACTS = """
    UPDATE session SET title = %s, is_empty = CASE WHEN %s THEN NOT %s ELSE is_empty END
     WHERE session_id = %s
"""


def refresh_identity(cursor: Any, session_id: str) -> None:
    """Link the chat to its account and project and set its title and ``is_empty``.

    Part of the indexer's refresh, in its transaction. Safe to repeat.

    Args:
        cursor: A cursor of the refresh's connection.
        session_id: The chat.
    """
    cursor.execute(_OWNER, (session_id,))
    owner = cursor.fetchone()
    link_chat(
        cursor,
        session_id,
        owner_account_uuid=owner[0] if owner else None,
        owner_organisation_uuid=owner[1] if owner else None,
    )
    cursor.execute(_TITLE, (session_id,))
    title = cursor.fetchone()
    cursor.execute(_MESSAGES, (session_id,))
    has_events, has_message = cursor.fetchone()
    cursor.execute(_SET_FACTS, (title[0] if title else None, has_events, has_message, session_id))
