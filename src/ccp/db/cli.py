"""Command line for schema migrations: ``ccp-db``.

Examples:
    ccp-db status                 # which migrations are applied
    ccp-db migrate                # apply everything pending
    ccp-db migrate --target 1     # apply up to version 1
    ccp-db rollback               # undo the latest migration
    ccp-db rollback --steps 2     # undo the latest two

The database comes from the POSTGRES_* variables (see docs/configuration.md).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ccp.db.connection import connect
from ccp.db.migrations import MigrationError, discover_migrations, migrate, rollback, status
from ccp.settings import SettingsError, load_database_settings

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
        connection = connect(load_database_settings())
        try:
            if args.command == "status":
                for migration, is_applied in status(connection, migrations):
                    print(f"[{'x' if is_applied else ' '}] {migration.label}")
            elif args.command == "migrate":
                done = migrate(connection, migrations, args.target)
                print("Applied: " + (", ".join(m.label for m in done) or "nothing to do"))
            else:
                undone = rollback(connection, migrations, args.steps)
                print("Rolled back: " + (", ".join(m.label for m in undone) or "nothing to undo"))
        finally:
            connection.close()
    except (MigrationError, SettingsError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0
