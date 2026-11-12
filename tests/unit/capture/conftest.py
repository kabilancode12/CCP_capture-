"""Fakes and fixtures shared by the capture client tests."""

from collections.abc import Sequence
from pathlib import Path

import pytest

from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.redaction.records import RedactionRecord


class FakeApi:
    """Records every call, in order; can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (kind, detail)
        self.chunks: dict[tuple[str, int], bytes] = {}  # (stream, offset) -> body
        self.attachments: dict[str, bytes] = {}
        self.redactions: list[RedactionRecord] = []
        self.environments: list[EnvironmentRecord] = []
        self.fail_with: Exception | None = None
        self.fail_on: str | None = None  # a kind: "chunk", "attachment" or "redactions"

    def _maybe_fail(self, kind: str) -> None:
        if self.fail_with is not None and self.fail_on in (None, kind):
            raise self.fail_with

    def put_chunk(self, session_id: str, stream: str, offset: int, body: bytes) -> bool:
        self._maybe_fail("chunk")
        self.calls.append(("chunk", f"{stream}@{offset}"))
        new = (stream, offset) not in self.chunks
        self.chunks[(stream, offset)] = body
        return new

    def put_attachment(self, digest: str, body: bytes) -> bool:
        self._maybe_fail("attachment")
        self.calls.append(("attachment", digest[:8]))
        new = digest not in self.attachments
        self.attachments[digest] = body
        return new

    def put_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        self._maybe_fail("environment")
        self.calls.append(("environment", str(len(record.to_payload()))))
        self.environments.append(record)

    def post_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        self._maybe_fail("redactions")
        self.calls.append(("redactions", str(len(records))))
        self.redactions.extend(records)


class Clock:
    """A clock that only moves when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state")
