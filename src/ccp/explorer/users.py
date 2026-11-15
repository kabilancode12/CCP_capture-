"""The explorer website's users (table ``ui_user``) and what each role may do.

Roles:

* ``developer``: sees the full content of the chats of their own Claude account (``account_id``);
  of everyone else's chats only counts.
* ``lead``: reads every chat and may approve or reject decisions.
* ``auditor``: like a lead, and may read the access log.
* ``admin``: everything, and manages users and labels.

Every function here runs in the caller's transaction; the caller commits.
"""

from __future__ import annotations

from dataclasses import dataclass

import pg8000.dbapi

from ccp.explorer.security import hash_password

ROLES = ("developer", "lead", "auditor", "admin")

_USER_COLUMNS = "user_id, email, full_name, role, account_id, password_hash, disabled"


class UserError(ValueError):
    """Raised for a user change that cannot be made (unknown role, duplicate e-mail ...)."""


@dataclass(frozen=True)
class UiUser:
    """One user of the website.

    Attributes:
        user_id: The row id.
        email: The login name.
        full_name: Shown as the reviewer of decisions; may be empty.
        role: One of :data:`ROLES`.
        account_id: The Claude account this person uses (``claude_account``), if linked.
        password_hash: The scrypt hash, or ``None`` if no password is set (cannot log in).
        disabled: A disabled user cannot log in and loses open sessions.
    """

    user_id: int
    email: str
    full_name: str
    role: str
    account_id: int | None
    password_hash: str | None
    disabled: bool

    @property
    def reads_everything(self) -> bool:
        """Leads, auditors and admins read every chat."""
        return self.role in ("lead", "auditor", "admin")

    @property
    def may_review(self) -> bool:
        """Leads, auditors and admins may approve or reject decisions."""
        return self.role in ("lead", "auditor", "admin")

    @property
    def is_admin(self) -> bool:
        """Admins manage users and labels."""
        return self.role == "admin"

    @property
    def reads_access_log(self) -> bool:
        """Admins and auditors read the access log."""
        return self.role in ("admin", "auditor")

    @property
    def reviewer_name(self) -> str:
        """The name recorded when this user reviews a decision."""
        return self.full_name or self.email

    def may_open_chat(self, chat_account_id: int | None) -> bool:
        """Whether this user may see the full content of a chat of the given Claude account."""
        if self.reads_everything:
            return True
        return self.account_id is not None and self.account_id == chat_account_id


def find_user(connection: pg8000.dbapi.Connection, user_id: int) -> UiUser | None:
    """The user with this id, or ``None``."""
    cursor = connection.cursor()
    cursor.execute(f"SELECT {_USER_COLUMNS} FROM ui_user WHERE user_id = %s", (user_id,))  # noqa: S608
    row = cursor.fetchone()
    return None if row is None else _user(row)


def find_user_by_email(connection: pg8000.dbapi.Connection, email: str) -> UiUser | None:
    """The user with this e-mail address (any case), or ``None``."""
    cursor = connection.cursor()
    cursor.execute(
        f"SELECT {_USER_COLUMNS} FROM ui_user WHERE lower(email) = lower(%s)",  # noqa: S608
        (email.strip(),),
    )
    row = cursor.fetchone()
    return None if row is None else _user(row)


def list_users(connection: pg8000.dbapi.Connection) -> list[UiUser]:
    """Every user, by e-mail address."""
    cursor = connection.cursor()
    cursor.execute(f"SELECT {_USER_COLUMNS} FROM ui_user ORDER BY lower(email)")  # noqa: S608
    return [_user(row) for row in cursor.fetchall()]


def create_user(
    connection: pg8000.dbapi.Connection,
    email: str,
    role: str,
    password: str,
    *,
    full_name: str = "",
    account_id: int | None = None,
) -> int:
    """Add a user and return their id.

    Raises:
        UserError: If the e-mail address is not plausible, already used, or the role is unknown.
        PasswordError: If the password is too short.
    """
    address = _checked_email(email)
    _check_role(role)
    password_hash = hash_password(password)
    if find_user_by_email(connection, address) is not None:
        raise UserError("A user with that e-mail address already exists")
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO ui_user (email, full_name, role, account_id, password_hash) "
        "VALUES (lower(%s), %s, %s, %s, %s) RETURNING user_id",
        (address, full_name.strip() or None, role, account_id, password_hash),
    )
    return int(cursor.fetchone()[0])


def set_role(connection: pg8000.dbapi.Connection, user_id: int, role: str) -> None:
    """Change a user's role.

    Raises:
        UserError: If the role is unknown or the user does not exist.
    """
    _check_role(role)
    _update(connection, "UPDATE ui_user SET role = %s WHERE user_id = %s", (role, user_id))


def link_account(connection: pg8000.dbapi.Connection, user_id: int, account_id: int | None) -> None:
    """Link a user to a Claude account (``None`` removes the link).

    Raises:
        UserError: If the user or the account does not exist.
    """
    if account_id is not None:
        cursor = connection.cursor()
        cursor.execute("SELECT 1 FROM claude_account WHERE account_id = %s", (account_id,))
        if cursor.fetchone() is None:
            raise UserError("No Claude account has that id")
    _update(
        connection, "UPDATE ui_user SET account_id = %s WHERE user_id = %s", (account_id, user_id)
    )


def set_disabled(connection: pg8000.dbapi.Connection, user_id: int, disabled: bool) -> None:
    """Disable a user (they cannot log in; open sessions end) or enable them again."""
    _update(connection, "UPDATE ui_user SET disabled = %s WHERE user_id = %s", (disabled, user_id))


def reset_password(connection: pg8000.dbapi.Connection, user_id: int, password: str) -> None:
    """Give a user a new password. Sessions opened with the old one end.

    Raises:
        PasswordError: If the password is too short.
        UserError: If the user does not exist.
    """
    _update(
        connection,
        "UPDATE ui_user SET password_hash = %s WHERE user_id = %s",
        (hash_password(password), user_id),
    )


def account_id_for_email(connection: pg8000.dbapi.Connection, email: str) -> int:
    """The Claude account with this e-mail address, added (known by e-mail only) if there is none.

    An account known only by e-mail address takes over its account uuid when its first chat is
    indexed (see :mod:`ccp.db.hierarchy`), so a link made before that keeps working.

    Raises:
        UserError: If the address is not plausible.
    """
    address = _checked_email(email)
    cursor = connection.cursor()
    cursor.execute(
        "SELECT account_id FROM claude_account WHERE lower(email) = lower(%s) "
        "ORDER BY account_uuid IS NULL, account_id LIMIT 1",
        (address,),
    )
    row = cursor.fetchone()
    if row is not None:
        return int(row[0])
    cursor.execute(
        "INSERT INTO claude_account (email) VALUES (lower(%s)) RETURNING account_id", (address,)
    )
    return int(cursor.fetchone()[0])


def record_access(
    connection: pg8000.dbapi.Connection, user_id: int | None, action: str, target: str | None
) -> None:
    """Write one row of the access log (``ui_access_log``)."""
    connection.cursor().execute(
        "INSERT INTO ui_access_log (user_id, action, target) VALUES (%s, %s, %s)",
        (user_id, action, target),
    )


def _update(connection: pg8000.dbapi.Connection, sql: str, parameters: tuple[object, ...]) -> None:
    """Run an UPDATE of one user; raise :class:`UserError` if no user has that id."""
    cursor = connection.cursor()
    cursor.execute(sql, parameters)
    if cursor.rowcount < 1:
        raise UserError("No user has that id")


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise UserError(f"The role must be one of: {', '.join(ROLES)}")


def _checked_email(email: str) -> str:
    """The address without outer spaces, or raise if it does not look like one."""
    address = email.strip()
    name, at, domain = address.partition("@")
    if not (name and at and domain) or any(c.isspace() for c in address) or len(address) > 254:
        raise UserError("That is not an e-mail address")
    return address


def _user(row: list[object] | tuple[object, ...]) -> UiUser:
    user_id, email, full_name, role, account_id, password_hash, disabled = row
    return UiUser(
        user_id=int(str(user_id)),
        email=str(email),
        full_name=str(full_name or ""),
        role=str(role),
        account_id=None if account_id is None else int(str(account_id)),
        password_hash=None if password_hash is None else str(password_hash),
        disabled=bool(disabled),
    )
