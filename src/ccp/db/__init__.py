"""Database access: connections and schema migrations.

Modules:
    connection: opens a PostgreSQL connection from :class:`ccp.settings.DatabaseSettings`.
    migrations: finds numbered SQL files and applies or rolls them back.
    cli: the ``ccp-db`` command (``migrate``, ``rollback``, ``status``).

The driver is ``pg8000``, a pure-Python PostgreSQL driver. It needs no compiled library, which
matters on machines where company policy blocks unsigned DLLs.
"""
