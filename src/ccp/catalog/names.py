"""Slugs and the folder layout of the catalog.

Layout (everything below ``catalog/``)::

    accounts/<account>__<account id8>/projects/<project>/sessions/<YYYY-MM-DD>__<title>__<chat id8>/

Every name is a slug: lowercase ``a-z``, ``0-9`` and dashes only, so a name is safe in any file
system and in any URL. The chat id8 (the first 8 characters of the chat id) is what ties a folder
to its chat, so a folder can be found again when the title or a label changes.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime

CATALOG_ROOT = "catalog"
ACCOUNTS_FOLDER = f"{CATALOG_ROOT}/accounts"
TITLE_MAX = 60
UNKNOWN_ACCOUNT = "unknown-account"
UNKNOWN_PROJECT = "unknown-project"
UNTITLED = "untitled"
UNDATED = "undated"

# catalog/accounts/<account>/projects/<project>/sessions/<chat folder>/<file...>
CHAT_FOLDER_PATTERN = re.compile(
    r"^(catalog/accounts/[^/]+/projects/[^/]+/(?:sessions|chats)/(?P<folder>[^/]+))/(?P<rest>.+)$"
)


def slugify(text: str | None, max_length: int | None = None, fallback: str = "") -> str:
    """Make a slug: accents folded, lowercase, everything but ``a-z0-9`` turned into one dash.

    Args:
        text: Any text (a title, a label, a name); None counts as empty.
        max_length: Cut the slug to at most this many characters (no dash is left at the end).
        fallback: Returned when nothing is left (empty text, or only symbols).
    """
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length].strip("-")
    return slug or fallback


def short_id(identifier: str) -> str:
    """The first 8 characters of an id, as a slug (a uuid gives its first block)."""
    return slugify(identifier[:8], fallback="x")


def account_folder(
    account_id: int | None, account_uuid: str | None, label: str | None, name: str | None
) -> str:
    """The folder name of an account: ``<label or name>__<id8>``.

    The id8 is the start of the account uuid when it is known, else the database id padded to 8
    digits; a chat without an account goes to ``unknown-account__00000000``.
    """
    if account_id is None:
        return f"{UNKNOWN_ACCOUNT}__00000000"
    ident = short_id(account_uuid) if account_uuid else f"{account_id:08d}"
    return f"{slugify(label or name, 60, 'account')}__{ident}"


def project_folder(label: str | None, name: str | None) -> str:
    """The folder name of a project: its label, else the slug of its name."""
    return slugify(label or name, 60, UNKNOWN_PROJECT)


def chat_folder(started: datetime | date | None, title: str | None, chat_id: str) -> str:
    """The folder name of a chat: ``<YYYY-MM-DD>__<title slug>__<chat id8>``."""
    if isinstance(started, datetime) and started.tzinfo:
        started = started.astimezone(UTC)  # the same day for everybody, wherever the server runs
    day = started.strftime("%Y-%m-%d") if started else UNDATED
    return f"{day}__{slugify(title, TITLE_MAX, UNTITLED)}__{short_id(chat_id)}"


def chat_id8_of_folder(folder: str) -> str:
    """The chat id8 at the end of a chat folder name."""
    return folder.rsplit("__", 1)[-1]
