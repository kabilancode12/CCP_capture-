"""Reading the index: the questions an audit asks (Step 3).

Modules:
    provenance: replays recorded edits to say which edit wrote a line (pure, no I/O).
    repository: the database queries behind the commands.
    cli: the ``ccp-audit`` command.

Everything here only *reads* the index of Step 2; nothing is written.
"""
