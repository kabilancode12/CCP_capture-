"""Dashboards on the captured data, set up as code (Step 3).

The dashboards are made in Metabase, a free dashboard tool that runs from the Compose file
(``--profile dashboards``). Making them by hand in its web interface would not be repeatable, so
this package does it through Metabase's API: the same questions and the same layout every time, on a
new machine or after the data was wiped.

Modules:
    definitions: the cards (each a SQL question on a dashboard view) and the layout, as data.
    client: a small client for Metabase's web API (standard library only).
    setup: the steps (first-time setup, database, cards, dashboard), each safe to repeat.
    cli: the ``ccp-dashboards`` command.

The data Metabase sees is limited by the database, not by this package: it connects with the
read-only role of ``ccp-db create-reader``, which can read only the dashboard views (migration 013).
"""
