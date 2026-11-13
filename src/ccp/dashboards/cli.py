"""The ``ccp-dashboards`` command (also ``python -m ccp.dashboards``).

    ccp-dashboards setup     set up Metabase (first time) and make or update the dashboard

Start Metabase first (``--profile dashboards``) and create the read-only database role
(``ccp-db create-reader``); see docs/dashboards.md. The command can be repeated: it brings
Metabase to the state the definitions describe.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ccp.dashboards.client import MetabaseClient, MetabaseError
from ccp.dashboards.setup import run_setup
from ccp.settings import SettingsError, load_dashboard_settings


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(prog="ccp-dashboards", description="Dashboards in Metabase.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="set up Metabase and make or update the dashboard")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command.

    Returns:
        0 on success, 1 if the settings are wrong or Metabase fails (the reason goes to stderr).
    """
    build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = load_dashboard_settings()
        address = run_setup(MetabaseClient(settings.metabase_url), settings)
    except (SettingsError, MetabaseError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Dashboard ready: {address}")
    return 0
