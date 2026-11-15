"""Command line for schema migrations: ``ccp-db``.

Examples:
    ccp-db status                 # which migrations are applied
    ccp-db migrate                # apply everything pending
    ccp-db migrate --target 1     # apply up to version 1
    ccp-db rollback               # undo the latest migration
    ccp-db rollback --steps 2     # undo the latest two
    ccp-db create-reader          # a read-only role for dashboards (password: CCP_READER_PASSWORD)
    ccp-db create-auditor         # a read-only role for everything (password: CCP_AUDITOR_PASSWORD)
    ccp-db labels                 # list Claude accounts and projects with their labels
    ccp-db label account someone@example.com claude1
    ccp-db label project billing billing-api          # by project name, key or folder
    ccp-db label project /home/dev/work/billing billing-api
    ccp-db unlabel account someone@example.com

The database comes from the POSTGRES_* variables (see docs/configuration.md).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import pg8000.dbapi

from ccp.db.connection import connect
from ccp.db.labels import (
    LabelError,
    list_labels,
    remove_label,
    set_account_label,
    set_project_label,
)
from ccp.db.migrations import MigrationError, discover_migrations, migrate, rollback, status
from ccp.db.readers import (
    DEFAULT_AUDITOR,
    DEFAULT_READER,
    ReaderError,
    create_auditor,
    create_reader,
)
from ccp.settings import (
    SettingsError,
    load_auditor_password,
    load_database_settings,
    load_reader_password,
)

DEFAULT_MIGRATIONS_DIR = Path("db/migrations")


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser with the ``status``, ``migrate`` and ``rollback`` commands."""
    parser = argparse.ArgumentParser(prog="ccp-db", description="Manage the database schema.")
    parser.add_argument(
        "--dir", type=Path, default=DEFAULT_MIGRATIONS_DIR, help="folder of migration files"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show which migrations are applied")
    up = commands.add_parser("migrate", help="apply pending migrations")
    up.add_argument("--target", type=int, help="stop after this version")
    down = commands.add_parser("rollback", help="undo the latest migrations")
    down.add_argument("--steps", type=int, default=1, help="how many to undo (default 1)")
    reader = commands.add_parser(
        "create-reader", help="create or update the read-only role for dashboards"
    )
    reader.add_argument("--name", default=DEFAULT_READER, help="role name (default %(default)s)")
    auditor = commands.add_parser(
        "create-auditor", help="create or update the read-only role that can read everything"
    )
    auditor.add_argument("--name", default=DEFAULT_AUDITOR, help="role name (default %(default)s)")
    commands.add_parser("labels", help="list Claude accounts and projects with their labels")
    label = commands.add_parser("label", help="give a Claude account or a project a label")
    label.add_argument("kind", choices=("account", "project"))
    label.add_argument(
        "key", help="the account's e-mail address or uuid, or the project's name, key or folder"
    )
    label.add_argument("label", help="the name shown on the dashboards, for example claude1")
    unlabel = commands.add_parser("unlabel", help="remove a label")
    unlabel.add_argument("kind", choices=("account", "project"))
    unlabel.add_argument(
        "key", help="the account's e-mail address or uuid, or the project's name, key or folder"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        0 on success, 1 if settings or a migration failed (the reason goes to stderr).
    """
    args = build_parser().parse_args(argv)
    try:
        migrations = discover_migrations(args.dir)
        settings = load_database_settings()
        connection = connect(settings)
        try:
            if args.command == "status":
                for migration, is_applied in status(connection, migrations):
                    print(f"[{'x' if is_applied else ' '}] {migration.label}")
            elif args.command == "create-reader":
                create_reader(connection, settings.name, load_reader_password().reveal(), args.name)
                print(f"Role {args.name} can read the dashboard views of {settings.name}.")
            elif args.command == "create-auditor":
                create_auditor(
                    connection, settings.name, load_auditor_password().reveal(), args.name
                )
                print(f"Role {args.name} can read every table and view of {settings.name}.")
            elif args.command in ("labels", "label", "unlabel"):
                _run_label_command(connection, args)
            elif args.command == "migrate":
                done = migrate(connection, migrations, args.target)
                print("Applied: " + (", ".join(m.label for m in done) or "nothing to do"))
            else:
                undone = rollback(connection, migrations, args.steps)
                print("Rolled back: " + (", ".join(m.label for m in undone) or "nothing to undo"))
        finally:
            connection.close()
    except (MigrationError, SettingsError, ReaderError, LabelError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def _run_label_command(connection: pg8000.dbapi.Connection, args: argparse.Namespace) -> None:
    """Run ``labels``, ``label`` or ``unlabel`` and print the outcome."""
    if args.command == "labels":
        for item in list_labels(connection):
            name = f"  ({item.name})" if item.name else ""
            print(f"{item.kind:8}  {item.label or '(no label)':20}  {item.key}{name}")
    elif args.command == "label":
        setter = set_account_label if args.kind == "account" else set_project_label
        setter(connection, args.key, args.label)
        print(f"The {args.kind} is now shown as {args.label.strip()}.")
    elif remove_label(connection, args.kind, args.key):
        print(f"Label of the {args.kind} removed.")
    else:
        print(f"That {args.kind} had no label.")
