"""Object storage for raw transcripts and attachments (MinIO, S3-compatible).

Modules:
    keys: the object-key layout, with validation of every outside value.
    object_store: a thin wrapper around the MinIO client.
    attachments: moves large, repeated attachment values out of transcript lines, stored once
        by content hash.

The names below are loaded **on first use**, not when the package is imported. The capture client
runs on developers' machines and needs only ``keys`` and ``attachments``; importing the MinIO
library (and its compiled dependencies) there would be slow and would force an installation that
only the server needs.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # for type checkers and editors only; nothing is imported at run time
    from ccp.storage.object_store import (
        ObjectNotFoundError,
        ObjectStore,
        StorageError,
        connect_store,
    )

__all__ = ["ObjectNotFoundError", "ObjectStore", "StorageError", "connect_store"]


def __getattr__(name: str) -> Any:
    """Load the object-store names from :mod:`ccp.storage.object_store` when first used."""
    if name in __all__:
        return getattr(importlib.import_module("ccp.storage.object_store"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
