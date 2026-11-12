"""A thin wrapper around the MinIO client, limited to what the platform needs.

Other code talks to :class:`ObjectStore` and never to the MinIO library, so the storage can be
replaced (another S3-compatible service, or a fake in tests) without touching callers. Errors
from the library are translated into :class:`StorageError`.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager

import urllib3
from minio import Minio
from minio.error import MinioException, S3Error

from ccp.settings import StorageSettings


class StorageError(RuntimeError):
    """Raised when the object store cannot be reached or refuses a request."""


class ObjectNotFoundError(StorageError):
    """Raised when a requested object does not exist."""


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Turn library errors into :class:`StorageError`; a missing object into its subclass."""
    try:
        yield
    except S3Error as error:
        if error.code in {"NoSuchKey", "NoSuchBucket"}:
            raise ObjectNotFoundError(f"Not found ({error.code})") from error
        raise StorageError(f"Storage request failed ({error.code})") from error
    except (MinioException, urllib3.exceptions.HTTPError) as error:
        raise StorageError(f"Storage unreachable or failed: {type(error).__name__}") from error


class ObjectStore:
    """Reads and writes objects in one bucket."""

    def __init__(self, client: Minio, bucket: str) -> None:
        """Wrap ``client``; all operations use ``bucket``."""
        self._client = client
        self._bucket = bucket

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist yet (safe to call every time)."""
        with _translate_errors():
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Store ``data`` under ``key``, replacing any existing object."""
        with _translate_errors():
            self._client.put_object(
                self._bucket, key, io.BytesIO(data), len(data), content_type=content_type
            )

    def get(self, key: str) -> bytes:
        """Return the object's bytes.

        Raises:
            ObjectNotFoundError: If there is no object with this key.
        """
        with _translate_errors():
            response = self._client.get_object(self._bucket, key)
            try:
                return bytes(response.read())
            finally:
                response.close()
                response.release_conn()

    def exists(self, key: str) -> bool:
        """Return True if an object with this key exists."""
        try:
            with _translate_errors():
                self._client.stat_object(self._bucket, key)
        except ObjectNotFoundError:
            return False
        return True

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Store ``data`` only if ``key`` is not used yet.

        Meant for content-addressed keys, where the same key always means the same bytes: if two
        writers race, both write identical content, so the outcome is still correct.

        Returns:
            True if the object was written, False if it already existed.
        """
        if self.exists(key):
            return False
        self.put(key, data)
        return True

    def list_keys(self, prefix: str) -> list[str]:
        """Return every key that starts with ``prefix``, in alphabetical order."""
        with _translate_errors():
            objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
            return sorted(str(obj.object_name) for obj in objects)

    def delete(self, key: str) -> None:
        """Delete an object. Deleting a key that does not exist is not an error."""
        with _translate_errors():
            self._client.remove_object(self._bucket, key)


def _http_client() -> urllib3.PoolManager:
    """HTTP settings for the storage client: short connect time, a few retries on server errors."""
    return urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=3.0, read=60.0),
        retries=urllib3.Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]),
    )


def connect_store(settings: StorageSettings, *, create_bucket: bool = True) -> ObjectStore:
    """Create an :class:`ObjectStore` from the storage settings.

    Args:
        settings: Endpoint, credentials and bucket name.
        create_bucket: Create the bucket if it is missing (normally what you want).

    Raises:
        StorageError: If the storage cannot be reached.
    """
    client = Minio(
        settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key.reveal(),
        secure=settings.secure,
        http_client=_http_client(),
    )
    store = ObjectStore(client, settings.bucket)
    if create_bucket:
        store.ensure_bucket()
    return store
