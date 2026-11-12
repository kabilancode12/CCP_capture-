"""The ingestion API: receives transcript chunks and stores them.

Modules:
    app: the web application (endpoints, authentication, idempotent chunk storage).
    chunks: checks that an uploaded chunk is well formed.
    repository: the database operations the API needs (a session record and an index job).
    server: the ``ccp-ingest`` command that starts the API.

Built on Starlette and uvicorn. FastAPI is not used because its pydantic-core dependency is a
compiled library that company Application Control policy blocks on some machines.
"""
