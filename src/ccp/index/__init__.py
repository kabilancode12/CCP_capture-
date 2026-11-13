"""The indexer: turns stored transcripts into rows in PostgreSQL (Step 2).

The index is *derived* data. Everything in it comes from the transcripts in MinIO, so it can be
deleted and rebuilt at any time, and indexing the same transcript twice gives the same rows.

Modules:
    parser: one transcript line to one :class:`~ccp.index.parser.ParsedEntry` (pure, no I/O).
    repository: the database operations of the indexer (claim a job, save a chunk's events).
    worker: indexes one session, and runs the job loop.
    cli: the ``ccp-index`` command.
"""
