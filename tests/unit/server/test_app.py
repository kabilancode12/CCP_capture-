"""Tests for the one-process server: API + indexer + mounts, and the command."""

import time
from collections.abc import Callable
from typing import Any, cast

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ccp.index.worker import ChunkStore, JobRepository
from ccp.ingest.app import IngestService
from ccp.ingest.closer import closer_lifespan
from ccp.ingest.repository import SessionRepository
from ccp.server import main as server_main
from ccp.server.indexer import IndexerThread
from ccp.server.plugins import PluginRegistry
from ccp.settings import Secret

from .conftest import FakeJobs, FakeSessionRepository, FakeStore, make_settings

TOKEN = "t" * 24
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SESSION = "6a1f2c3d-0000-4000-8000-000000000001"


class FakeCloser:
    def __init__(self) -> None:
        self.calls = 0

    def close_idle_sessions(self, idle_minutes: int) -> list[str]:
        self.calls += 1
        return []


def build(
    registry: PluginRegistry | None = None,
) -> tuple[Starlette, FakeJobs, IndexerThread, list[str]]:
    jobs = FakeJobs()
    store = FakeStore()
    called: list[str] = []
    registry = registry or PluginRegistry()
    registry.add_after_index(called.append)
    service = IngestService(
        token=Secret(TOKEN),
        store=store,
        repository=cast(SessionRepository, FakeSessionRepository(jobs)),
        max_chunk_bytes=1024,
    )
    indexer = IndexerThread(
        cast(ChunkStore, store),
        cast(JobRepository, jobs),
        poll_seconds=0.01,
        callbacks=registry.after_index,
    )
    closer = closer_lifespan(FakeCloser(), 30, 60)
    app = server_main.build_server_app(service, closer=closer, indexer=indexer, registry=registry)
    return app, jobs, indexer, called


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_health_is_unchanged_and_the_indexer_runs_while_the_server_is_up() -> None:
    app, _, indexer, _ = build()
    assert not indexer.running
    with TestClient(app) as client:
        assert indexer.running
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok", "storage": "ok"}
    assert not indexer.running  # graceful shutdown stopped it


def test_an_uploaded_chunk_is_indexed_and_the_callback_runs() -> None:
    app, jobs, _, called = build()
    body = b'{"type":"user"}\n'
    with TestClient(app) as client:
        response = client.put(
            f"/v1/sessions/{SESSION}/streams/main/chunks/0", content=body, headers=AUTH
        )
        assert response.status_code == 201
        assert wait_for(lambda: called == [SESSION])
    assert jobs.done == [SESSION]
    assert jobs.saved_chunks == [f"sessions/{SESSION}/main/{0:012d}.jsonl"]


def test_the_api_still_refuses_a_wrong_token() -> None:
    app, jobs, _, _ = build()
    with TestClient(app) as client:
        response = client.put(
            f"/v1/sessions/{SESSION}/streams/main/chunks/0",
            content=b"{}\n",
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401
    assert jobs.queued == [] and jobs.done == []


def test_plugin_mounts_are_served_next_to_the_api() -> None:
    async def page(request: Request) -> PlainTextResponse:
        return PlainTextResponse("explorer " + request.url.path)

    registry = PluginRegistry()
    registry.add_mount("/explorer", Starlette(routes=[Route("/", page), Route("/x", page)]))
    app, _, _, _ = build(registry)
    with TestClient(app) as client:
        assert client.get("/explorer/x").text == "explorer /explorer/x"
        assert client.get("/health").status_code == 200
        assert client.get("/nothing").status_code == 404


def test_shutdown_waits_for_the_running_job_not_for_the_queue() -> None:
    app, jobs, indexer, _ = build()
    with TestClient(app):
        jobs.queue("s-a")
        assert wait_for(lambda: jobs.done == ["s-a"])
    assert not indexer.running


# ---------------------------------------------------------------- the command


def test_a_pending_migration_stops_the_server_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(server_main, "load_settings", make_settings)
    monkeypatch.setattr(server_main, "_merge_env", lambda env, file: {})
    monkeypatch.setattr(server_main, "schema_problem", lambda database, path: "pending: 017")
    monkeypatch.setattr(
        server_main, "connect_store", lambda settings: pytest.fail("must not connect")
    )
    assert server_main.main([]) == 1
    assert "pending: 017" in capsys.readouterr().err


def test_a_settings_error_is_reported_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("CCP_INGEST_TOKEN", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir("/")  # no .env file here
    assert server_main.main([]) == 1
    assert "CCP_INGEST_TOKEN" in capsys.readouterr().err


def test_the_command_serves_the_wired_app(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: dict[str, Any] = {}
    sentinel = object()
    monkeypatch.setattr(server_main, "load_settings", make_settings)
    monkeypatch.setattr(server_main, "_merge_env", lambda env, file: {"X": "1"})
    monkeypatch.setattr(server_main, "schema_problem", lambda database, path: None)
    monkeypatch.setattr(server_main, "connect_store", lambda settings: FakeStore())
    monkeypatch.setattr(
        server_main,
        "_build",
        lambda settings, store, env, poll: (ran.update(env=env, poll=poll), sentinel)[1],
    )
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.update(app=app, **kw))
    assert server_main.main(["--poll-seconds", "2"]) == 0
    assert ran["app"] is sentinel and ran["poll"] == 2.0 and ran["env"] == {"X": "1"}
    assert ran["host"] == "127.0.0.1" and ran["port"] == 8000


def test_the_migrate_flag_applies_migrations_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(server_main, "load_settings", make_settings)
    monkeypatch.setattr(server_main, "_merge_env", lambda env, file: {})
    monkeypatch.setattr(server_main, "_apply_migrations", lambda s, d: order.append("migrate"))
    monkeypatch.setattr(server_main, "schema_problem", lambda database, path: order.append("check"))
    monkeypatch.setattr(server_main, "connect_store", lambda settings: FakeStore())
    monkeypatch.setattr(server_main, "_build", lambda *a: order.append("build"))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: order.append("serve"))
    assert server_main.main(["--migrate"]) == 0
    assert order == ["migrate", "check", "build", "serve"]


def test_the_migrate_switch_in_the_environment_works_too(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(server_main, "load_settings", make_settings)
    monkeypatch.setattr(server_main, "_merge_env", lambda env, file: {"CCP_SERVER_MIGRATE": "true"})
    monkeypatch.setattr(server_main, "_apply_migrations", lambda s, d: order.append("migrate"))
    monkeypatch.setattr(server_main, "schema_problem", lambda database, path: None)
    monkeypatch.setattr(server_main, "connect_store", lambda settings: FakeStore())
    monkeypatch.setattr(server_main, "_build", lambda *a: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    assert server_main.main([]) == 0
    assert order == ["migrate"]
