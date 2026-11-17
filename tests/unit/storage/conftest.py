import pytest

from ccp.storage import ObjectNotFoundError


class MemoryStore:
    """A dictionary standing in for the object store (same methods the code under test uses)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_if_absent(self, key: str, data: bytes) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = data
        return True

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ObjectNotFoundError("missing")
        return self.objects[key]


@pytest.fixture
def memory_store() -> MemoryStore:
    return MemoryStore()
