"""Object storage for raw transcripts and attachments (MinIO, S3-compatible).

Modules:
    keys: the object-key layout, with validation of every outside value.
    object_store: a thin wrapper around the MinIO client.
    attachments: moves large, repeated attachment values out of transcript lines, stored once
        by content hash.
"""

from ccp.storage.object_store import ObjectNotFoundError, ObjectStore, StorageError, connect_store

__all__ = ["ObjectNotFoundError", "ObjectStore", "StorageError", "connect_store"]
