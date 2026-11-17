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

``PUT /v1/sessions/{session_id}/environment``
    Store where a session ran: Claude Code version, surface, shell, OS, host and user names, and
    the account (see ``ccp.environment``). A JSON object with any of the known fields; fields that
    are present replace the stored value, missing ones leave it unchanged, so the record can be
    sent in parts and repeated safely. 200 with ``{"recorded": n}``; 400 bad id, 401, 413,
    422 malformed, 503 database down. This is personal data: restrict access to it.

``POST /v1/sessions/{session_id}/lifecycle``
    Record that a session of the chat started or ended (SessionStart / SessionEnd hooks), as
    ``{"kind": "start"|"end", "run_key", "at", "source" or "reason", "commit"}`` (see
    ``ccp.lifecycle``). The run key makes it idempotent: the same event again changes nothing, and
    a start and an end with the same key are one row of ``chat_session``, whichever comes first.
    200 with ``{"recorded": true, "seq": n}`` (the session's number in the chat); 400 bad id, 401,
    409 the run key belongs to another chat, 413 larger than 4 KB, 422 malformed, 503 database
    down.

``PUT /v1/sessions/{session_id}/tool-results/{name}``
    Store one tool-result file: a large tool output that Claude Code keeps in
    ``<chat folder>/tool-results/<name>`` instead of the transcript. The body is the file's text,
    already redacted by the client (UTF-8). Stored at ``sessions/<id>/tool-results/<name>``; a
    changed file is stored next to it as ``<name>@<first 16 hex of its SHA-256>``: nothing stored
    is ever replaced.

    * 201 stored for the first time; 200 already stored with identical content (nothing written)
      or a changed version stored next to it.
    * 400 bad id or name, 401, 413 too large, 422 not UTF-8 text, 503 storage down.

    Every success returns ``{"stored": bool, "key": "<object key>"}``: ``stored`` is False if it
    was already there unchanged; ``key`` is where this content is stored (the file's key or the key
    of its changed version), so that the client can report its redactions against it.

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

import hashlib
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

from ccp.environment import EnvironmentRecordError, parse_environment
from ccp.ingest.attachments import AttachmentError, validate_attachment
from ccp.ingest.chunks import ChunkError, validate_chunk
from ccp.ingest.redactions import RedactionReportError, parse_report
from ccp.ingest.repository import RepositoryError, RunKeyConflictError, SessionRepository
from ccp.lifecycle import LifecycleEventError, parse_lifecycle
from ccp.settings import Secret
from ccp.shadow import ShadowCommitError, parse_shadow_commit
from ccp.storage import StorageError
from ccp.storage.keys import (
    attachment_key,
    session_prefix,
    tool_result_key,
    tool_result_version_key,
    transcript_chunk_key,
)

logger = logging.getLogger("ccp.ingest")

CHUNK_CONTENT_TYPE = "application/x-ndjson"
ATTACHMENT_CONTENT_TYPE = "application/json"
TOOL_RESULT_CONTENT_TYPE = "text/plain; charset=utf-8"

# Largest lifecycle event accepted (a few hundred bytes in practice).
MAX_LIFECYCLE_BYTES = 4096
# Largest snapshot record accepted (up to 500 file entries).
MAX_SNAPSHOT_BYTES = 262_144


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
                    "/v1/sessions/{session_id}/environment",
                    self.put_environment,
                    methods=["PUT"],
                ),
                Route(
                    "/v1/sessions/{session_id}/redactions",
                    self.post_redactions,
                    methods=["POST"],
                ),
                Route(
                    "/v1/sessions/{session_id}/lifecycle",
                    self.post_lifecycle,
                    methods=["POST"],
                ),
                Route(
                    "/v1/sessions/{session_id}/snapshots",
                    self.post_snapshot,
                    methods=["POST"],
                ),
                Route(
                    "/v1/sessions/{session_id}/tool-results/{name}",
                    self.put_tool_result,
                    methods=["PUT"],
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

    async def put_environment(self, request: Request) -> JSONResponse:
        """Store or merge a session's environment record (see the module description)."""
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
            record = parse_environment(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(422, "environment is not valid JSON")
        except EnvironmentRecordError as error:
            return _error(422, str(error))

        try:
            await run_in_threadpool(self._repository.record_environment, session_id, record)
        except RepositoryError as error:
            logger.warning("environment for %s not saved: %s", session_id, error)
            return _error(503, "database temporarily unavailable")
        return JSONResponse({"recorded": len(record.to_payload())})

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

    async def post_lifecycle(self, request: Request) -> JSONResponse:
        """Record a session start or end of a chat (see the module description)."""
        if not self._is_authorised(request):
            return await self._refuse(
                request, 401, "missing or invalid token", {"WWW-Authenticate": "Bearer"}
            )

        session_id = request.path_params["session_id"]
        try:
            session_prefix(session_id)  # validates the id
        except ValueError as error:
            return await self._refuse(request, 400, str(error))

        body = await self._read_body(request, MAX_LIFECYCLE_BYTES)
        if isinstance(body, JSONResponse):
            return body
        try:
            event = parse_lifecycle(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(422, "lifecycle event is not valid JSON")
        except LifecycleEventError as error:
            return _error(422, str(error))

        try:
            seq = await run_in_threadpool(self._repository.record_lifecycle, session_id, event)
        except RunKeyConflictError as error:
            return _error(409, str(error))
        except RepositoryError as error:
            logger.warning("lifecycle event for %s not saved: %s", session_id, error)
            return _error(503, "database temporarily unavailable")
        return JSONResponse({"recorded": True, "seq": seq})

    async def post_snapshot(self, request: Request) -> JSONResponse:
        """Record one snapshot commit of the capture repository (see :mod:`ccp.shadow`)."""
        if not self._is_authorised(request):
            return await self._refuse(
                request, 401, "missing or invalid token", {"WWW-Authenticate": "Bearer"}
            )
        session_id = request.path_params["session_id"]
        try:
            session_prefix(session_id)  # validates the id
        except ValueError as error:
            return await self._refuse(request, 400, str(error))
        body = await self._read_body(request, MAX_SNAPSHOT_BYTES)
        if isinstance(body, JSONResponse):
            return body
        try:
            commit = parse_shadow_commit(json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(422, "snapshot record is not valid JSON")
        except ShadowCommitError as error:
            return _error(422, str(error))
        try:
            new = await run_in_threadpool(self._repository.record_shadow_commit, session_id, commit)
        except RepositoryError as error:
            logger.warning("snapshot for %s not saved: %s", session_id, error)
            return _error(503, "database temporarily unavailable")
        return JSONResponse({"recorded": True, "new": new})

    async def put_tool_result(self, request: Request) -> JSONResponse:
        """Store one redacted tool-result file (see the module description)."""
        if not self._is_authorised(request):
            return await self._refuse(
                request, 401, "missing or invalid token", {"WWW-Authenticate": "Bearer"}
            )

        try:
            key = tool_result_key(request.path_params["session_id"], request.path_params["name"])
        except ValueError as error:
            return await self._refuse(request, 400, str(error))

        body = await self._read_body(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            return _error(422, "a tool result must be UTF-8 text")

        try:
            created, changed, stored_at = await run_in_threadpool(self._save_tool_result, key, body)
        except StorageError as error:
            logger.warning("tool result %s not saved: %s", key, error)
            return _error(503, "storage temporarily unavailable")
        return JSONResponse(
            {"stored": changed, "key": stored_at}, status_code=201 if created else 200
        )

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

    async def _read_body(self, request: Request, limit: int | None = None) -> bytes | JSONResponse:
        """Read the request body, or return a 413 response if it is larger than the limit.

        The announced Content-Length is checked first, so an oversized upload is refused before
        it is read.

        Args:
            request: The request.
            limit: Largest body accepted; defaults to the largest chunk.
        """
        limit = self._max_chunk_bytes if limit is None else min(limit, self._max_chunk_bytes)
        too_large = _error(413, f"body is larger than {limit} bytes")
        if _declared_size(request) > limit:
            return too_large
        body = await request.body()
        return too_large if len(body) > limit else body

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

    def _save_tool_result(self, key: str, body: bytes) -> tuple[bool, bool, str]:
        """Store a tool-result file unless the identical file is already there.

        Raw data is never replaced: a file that changed is stored as a new version next to the
        first one (:func:`ccp.storage.keys.tool_result_version_key`).

        Returns:
            ``(created, changed, stored_at)``: whether the file's key is new, whether anything was
            written, and the key that holds this content.
        """
        if not self._store.exists(key):
            self._store.put(key, body, TOOL_RESULT_CONTENT_TYPE)
            return True, True, key
        if self._store.get(key) == body:
            return False, False, key
        version = tool_result_version_key(key, hashlib.sha256(body).hexdigest())
        if self._store.exists(version):
            return False, False, version
        self._store.put(version, body, TOOL_RESULT_CONTENT_TYPE)
        return False, True, version

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
