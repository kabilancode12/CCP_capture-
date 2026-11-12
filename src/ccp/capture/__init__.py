"""The capture client: runs on a developer's machine and uploads Claude Code transcripts.

Modules:
    reader: reads the new, complete lines of a transcript file.
    pipeline: cleans one chunk (redaction, attachment de-duplication, checks).
    api: the connection to the ingestion API (standard library only).
    state: the small amount of state kept locally (how far each stream has been uploaded).
    client: the sync logic that ties these together.
    cli: the ``ccp-capture`` command.

The client needs no database or storage credentials: it talks only to the ingestion API.
"""
