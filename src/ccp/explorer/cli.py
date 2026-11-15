"""The explorer command line (``python -m ccp.explorer``).

    create-user EMAIL --role admin|lead|auditor|developer [--account-email E] [--name NAME]
        Add a user. The password comes from ``CCP_EXPLORER_PASSWORD`` or is asked for (twice).
    serve [--host 127.0.0.1] [--port 8090]
        Serve the website on its own, for testing. Needs ``CCP_EXPLORER_SECRET``.

Settings: the ``POSTGRES_*`` variables, and ``CCP_EXPLORER_SECRET`` (at least 16 characters) for
``serve``. The database must already have migration 016 applied.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import uvicorn

from ccp.db.connection import connect
from ccp.explorer.app import create_app
from ccp.explorer.security import MIN_SECRET_LENGTH, PasswordError
from ccp.explorer.users import ROLES, UserError, account_id_for_email, create_user
from ccp.settings import DEFAULT_ENV_FILE, SettingsError, load_database_settings, read_env_file


def build_parser() -> argparse.ArgumentParser:
    """The argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-explorer", description="CCP Explorer website.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user", help="add a user of the website")
    create.add_argument("email")
    create.add_argument("--role", required=True, choices=ROLES)
    create.add_argument("--account-email", help="the Claude account's e-mail (developers)")
    create.add_argument("--name", default="", help="the name shown as reviewer")
    serve = commands.add_parser("serve", help="serve the website on its own (for testing)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8090)
    return parser


def load_secret(
    env: Mapping[str, str] | None = None, env_file: Path | None = DEFAULT_ENV_FILE
) -> str:
    """Read ``CCP_EXPLORER_SECRET`` (the environment wins over the ``.env`` file).

    Raises:
        SettingsError: If it is missing or shorter than the minimum.
    """
    merged = dict(read_env_file(env_file)) if env_file is not None else {}
    merged.update(os.environ if env is None else env)
    secret = merged.get("CCP_EXPLORER_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise SettingsError(
            f"CCP_EXPLORER_SECRET is missing or shorter than {MIN_SECRET_LENGTH} characters"
        )
    return secret


def _password() -> str:
    """The new user's password: from ``CCP_EXPLORER_PASSWORD``, else asked for twice."""
    given = os.environ.get("CCP_EXPLORER_PASSWORD")
    if given:
        return given
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat the password: "):
        raise UserError("The two passwords differ")
    return first


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line. Returns 0 on success, 1 on a problem (the reason goes to stderr)."""
    args = build_parser().parse_args(argv)
    try:
        database = load_database_settings()
        if args.command == "serve":
            app = create_app(database, secret=load_secret())
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
            return 0
        password = _password()
        connection = connect(database)
        try:
            account_id = (
                account_id_for_email(connection, args.account_email) if args.account_email else None
            )
            create_user(
                connection,
                args.email,
                args.role,
                password,
                full_name=args.name,
                account_id=account_id,
            )
            connection.commit()
        finally:
            connection.close()
        print(f"User {args.email.strip().lower()} created with role {args.role}.")
    except (SettingsError, UserError, PasswordError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0
