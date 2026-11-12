"""The ingestion API: receives transcript chunks from developers' machines.

Endpoints:

``GET /health``
    Liveness and dependency check (no token needed). 200 if the database and the object store
    answer, otherwise 503 with the name of the failing part.

``PUT /v1/sessions/{session_id}/streams/{stream}/chunks/{offset}``
    Upload one chunk. ``stream`` is ``main`` or ``agent-<agent_id>``; ``offset`` is the byte
    position of the chunk in the source file. The body is the chunk (complete JSON lines).
    Requires ``Authorization: Bearer <token>``.

    The same URL always means the same chunk, so **uploads are idempotent**:

    * 201: stored for the first time.
    * 200: already stored with identical content (a retry); nothing is written again.
    * 409: a *different* chunk is already stored at this offset.
    * 400 bad id, 401 bad token, 413 too large, 422 malformed chunk, 503 storage or database down.

    Every success returns ``{"stored": bool}``. The client keeps its own position in the source
    file: the uploaded text is redacted, so its size is not the size of the source bytes.

``POST /v1/sessions/{session_id}/redactions``
    Report where redaction rules fired (rule id and location, never the removed value), as
    ``{"records": [{"rule_id", "location", "occurrences"}]}``. Safe to repeat. 200 with
    ``{"recorded": n}``; 400 bad id, 401, 413, 422 malformed report, 503 database down.

``PUT /v1/attachments/{digest}``
    Upload one attachment body (compact JSON) under its SHA-256 digest. Large, repeated
    attachment values are stored once, by content (see ``ccp.storage.attachments``); developers'
    machines send them here, so they never need storage credentials. The server checks that the
    body really hashes to ``digest``. Requires the bearer token.

    * 201 stored for the first time; 200 already stored (nothing written).
    * 400 bad digest, 401 bad token, 413 too large, 422 not JSON or digest mismatch,
      503 storage down.

    Every success returns ``{"stored": bool}``.

The server assumes one uploader per session (a developer's machine). Two machines sending
different chunks for the same offset at the same moment is not supported.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Lifespan

from ccp.ingest.attachments import AttachmentError, validate_attachment
from ccp.ingest.chunks import ChunkError, validate_chunk
from ccp.ingest.redactions import RedactionReportError, parse_report
from ccp.ingest.repository import RepositoryError, SessionRepository
from ccp.settings import Secret
from ccp.storage import StorageError
from ccp.storage.keys import attachment_key, session_prefix, transcript_chunk_key

logger = logging.getLogger("ccp.ingest")

CHUNK_CONTENT_TYPE = "application/x-ndjson"
ATTACHMENT_CONTENT_TYPE = "application/json"


class ChunkStore(Protocol):
    """The part of the object store the API needs."""

    def put(self, key: str, data: bytes, content_type: str = ...) -> None:
        """Store ``data`` under ``key``."""

    def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``."""

    def exists(self, key: str) -> bool:
        """Return True if ``key`` exists."""


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """Build a JSON error response: ``{"error": "..."}``."""
    return JSONResponse({"error": message}, status_code=status, headers=headers)


class IngestService:
    """The API's handlers and the services they use."""

    def __init__(
        self,
        *,
        token: Secret,
        store: ChunkStore,
        repository: SessionRepository,
        max_chunk_bytes: int,
    ) -> None:
        """Create the service.

        Args:
            token: The bearer token clients must send.
            store: Where chunks are stored.
            repository: The database operations the API needs.
            max_chunk_bytes: Largest chunk accepted.
        """
        self._token = token
        self._store = store
        self._repository = repository
        self._max_chunk_bytes = max_chunk_bytes

    def build_app(self, lifespan: Lifespan[Starlette] | None = None) -> Starlette:
        """Return the web application.

        Args:
            lifespan: Optional start-up and shut-down hook (the server uses it to run the
                idle-session closer in the background).
        """
        return Starlette(
            lifespan=lifespan,
            routes=[
                Route("/health", self.health, methods=["GET"]),
                Route(
                    "/v1/sessions/{session_id}/streams/{stream}/chunks/{offset:int}",
                    self.put_chunk,
                    methods=["PUT"],
                ),
                Route("/v1/attachments/{digest}", self.put_attachment, methods=["PUT"]),
                Route(
                    "/v1/sessions/{session_id}/redactions",
                    self.post_redactions,
                    methods=["POST"],
                ),
            ],
        )

    # ------------------------------------------------------------ handlers

    async def health(self, request: Request) -> JSONResponse:
        """Report whether the database and the object store answer."""
        database = await run_in_threadpool(self._check_database)
        storage = await run_in_threadpool(self._check_storage)
        body = {"status": "ok" if database and storage else "degraded"}
        body["database"] = "ok" if database else "unavailable"
        body["storage"] = "ok" if storage else "unavailable"
        return JSONResponse(body, status_code=200 if database and storage else 503)

    async def put_chunk(self, request: Request) -> JSONResponse:
        """Store one uploaded chunk (see the module description for the status codes)."""
        if not self._is_authorised(request):
            return await self._refuse(
                request, 401, "missing or invalid token", {"WWW-Authenticate": "Bearer"}
            )

        session_id = request.path_params["session_id"]
        stream = request.path_params["stream"]
        offset = request.path_params["offset"]
        try:
            key = transcript_chunk_key(session_id, stream, offset)
        except ValueError as error:
            return await self._refuse(request, 400, str(error))

        body = await self._read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            validate_chunk(body)
        except ChunkError as error:
            return _error(422, str(error))

        try:
            created = await run_in_threadpool(self._save, key, session_id, body)
        except (StorageError, RepositoryError) as error:
            logger.warning("chunk %s not saved: %s", key, error)
            return _error(503, "storage or database temporarily unavailable")
        if created is None:
            return _error(409, "a different chunk is already stored at this offset")
        return JSONResponse({"stored": created}, status_code=201 if created else 200)

    async def put_attachment(self, request: Request) -> JSONResponse:
        """Store one attachment body under its digest (see the module description)."""
        if not self._is_authorised(request):
            return await self._refuse(
                request, 401, "missing or invalid token", {"WWW-Authenticate": "Bearer"}
            )

        digest = request.path_params["digest"]
        try:
            key = attachment_key(digest)
        except ValueError as error:
            return await self._refuse(request, 400, str(error))

        body = await self._read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            validate_attachment(digest, body)
        except AttachmentError as error:
            return _error(422, str(error))

        try:
            created = await run_in_threadpool(self._save_attachment, key, body)
        except StorageError as error:
            logger.warning("attachment %s not saved: %s", key, error)
            return _error(503, "storage temporarily unavailable")
        return JSONResponse({"stored": created}, status_code=201 if created else 200)

    async def post_redactions(self, request: Request) -> JSONResponse:
        """Record where the client's redaction rules fired (see the module description)."""
        if not self._is_authorised(request):
            return await self._refuse(
                request, 401, "missing or invalid token", {"WWW-Authenticate": "Bearer"}
            )

        session_id = request.path_params["session_id"]
        try:
            session_prefix(session_id)  # validates the id
        except ValueError as error:
            return await self._refuse(request, 400, str(error))

        body = await self._read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            records = parse_report(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(422, "report is not valid JSON")
        except RedactionReportError as error:
            return _error(422, str(error))

        try:
            await run_in_threadpool(self._repository.record_redactions, session_id, records)
        except RepositoryError as error:
            logger.warning("redactions for %s not saved: %s", session_id, error)
            return _error(503, "database temporarily unavailable")
        return JSONResponse({"recorded": len(records)})

    # ------------------------------------------------------------ helpers

    async def _refuse(
        self, request: Request, status: int, message: str, headers: dict[str, str] | None = None
    ) -> JSONResponse:
        """Refuse a request before its body was read, without resetting the connection.

        If the server answers and closes the connection while the request body is still unread,
        the operating system may reset the connection (seen on Windows), and the client sees a
        network error instead of the real status: a permanent refusal would look like a temporary
        outage. So the body is read and dropped first (only if it is within the size limit; a
        larger body is refused by :meth:`_read_body` without being read, as before).
        """
        if _declared_size(request) <= self._max_chunk_bytes:
            async for _ in request.stream():
                pass
        return _error(status, message, headers)

    async def _read_body(self, request: Request) -> bytes | JSONResponse:
        """Read the request body, or return a 413 response if it is larger than the limit.

        The announced Content-Length is checked first, so an oversized upload is refused before
        it is read.
        """
        too_large = _error(413, f"body is larger than {self._max_chunk_bytes} bytes")
        if _declared_size(request) > self._max_chunk_bytes:
            return too_large
        body = await request.body()
        return too_large if len(body) > self._max_chunk_bytes else body

    def _is_authorised(self, request: Request) -> bool:
        """Check the bearer token in constant time (so timing does not reveal it)."""
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(credentials.strip().encode(), self._token.reveal().encode())

    def _save(self, key: str, session_id: str, body: bytes) -> bool | None:
        """Store the chunk and record it in the database.

        Returns:
            True if newly stored, False if an identical chunk was already there, ``None`` if a
            different chunk is already stored at this key (a conflict).

        The database step runs on every call, including retries, so a crash between the two
        steps heals itself the next time the client sends the chunk.
        """
        created = not self._store.exists(key)
        if created:
            self._store.put(key, body, CHUNK_CONTENT_TYPE)
        elif self._store.get(key) != body:
            return None
        self._repository.record_chunk(session_id)
        return created

    def _save_attachment(self, key: str, body: bytes) -> bool:
        """Store the body unless it is already there.

        The key is the content's hash, so an object that already exists is identical.

        Returns:
            True if newly stored, False if it already existed.
        """
        if self._store.exists(key):
            return False
        self._store.put(key, body, ATTACHMENT_CONTENT_TYPE)
        return True

    def _check_database(self) -> bool:
        try:
            self._repository.ping()
        except RepositoryError:
            return False
        return True

    def _check_storage(self) -> bool:
        try:
            self._store.exists("health-check")  # False for a missing key is fine; down raises
        except StorageError:
            return False
        return True


def _declared_size(request: Request) -> int:
    """Return the Content-Length the client announced, or 0 if absent or not a number."""
    try:
        return int(request.headers.get("content-length", "0"))
    except ValueError:
        return 0
