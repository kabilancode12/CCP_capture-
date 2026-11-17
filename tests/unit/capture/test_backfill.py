"""Tests for ``ccp-capture backfill``: temporary folders and a fake API only, never real chats."""

import json
import os
import time
from pathlib import Path

import pytest

from ccp.capture import cli
from ccp.capture.api import ApiUnavailableError
from ccp.capture.backfill import ProjectReport, backfill, find_transcripts, pending_bytes
from ccp.capture.client import CaptureClient
from ccp.capture.state import StateStore

from .conftest import FakeApi

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
S3 = "33333333-3333-3333-3333-333333333333"


def chat(projects: Path, project: str, session: str, lines: int = 2) -> Path:
    """Write a small invented transcript."""
    folder = projects / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session}.jsonl"
    rows = [
        json.dumps({"type": "user", "n": i, "message": {"content": "hello"}}) for i in range(lines)
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def client(api: FakeApi, state: StateStore) -> CaptureClient:
    return CaptureClient(api, state)


def test_find_transcripts_looks_one_level_down_and_ignores_other_files(projects: Path) -> None:
    chat(projects, "-home-me-a", S1)
    chat(projects, "-home-me-b", S2)
    (projects / "-home-me-a" / "notes.txt").write_text("x", encoding="utf-8")
    (projects / "-home-me-a" / S1).mkdir()
    (projects / "-home-me-a" / S1 / "deep.jsonl").write_text("{}\n", encoding="utf-8")
    (projects / "-home-me-a" / "bad name.jsonl").write_text("{}\n", encoding="utf-8")
    found = find_transcripts(projects)
    assert [(t.project, t.session_id) for t in found] == [("-home-me-a", S1), ("-home-me-b", S2)]
    assert find_transcripts(projects / "missing") == []


def test_since_keeps_only_recently_changed_chats(projects: Path) -> None:
    old = chat(projects, "p", S1)
    chat(projects, "p", S2)
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))
    assert [t.session_id for t in find_transcripts(projects, since_days=3)] == [S2]
    assert len(find_transcripts(projects, since_days=30)) == 2


def test_backfill_uploads_every_chat_and_counts_per_project(
    projects: Path, client: CaptureClient, state: StateStore, api: FakeApi
) -> None:
    chat(projects, "p1", S1)
    chat(projects, "p1", S2)
    chat(projects, "p2", S3)
    seen: list[tuple[str, ProjectReport]] = []
    report = backfill(client, state, projects, on_project=lambda n, r: seen.append((n, r)))
    assert report.ok
    assert {n: (r.found, r.uploaded) for n, r in report.projects.items()} == {
        "p1": (2, 2),
        "p2": (1, 1),
    }
    assert [n for n, _ in seen] == ["p1", "p2"]
    assert len([c for c in api.calls if c[0] == "chunk"]) == 3


def test_a_second_run_uploads_nothing(
    projects: Path, client: CaptureClient, state: StateStore, api: FakeApi
) -> None:
    chat(projects, "p1", S1)
    backfill(client, state, projects)
    api.calls.clear()
    report = backfill(client, state, projects)
    assert report.ok
    assert report.projects["p1"].uploaded == 0 and report.projects["p1"].unchanged == 1
    assert [c for c in api.calls if c[0] == "chunk"] == []


def test_a_chat_that_grew_sends_only_the_new_lines(
    projects: Path, client: CaptureClient, state: StateStore, api: FakeApi
) -> None:
    path = chat(projects, "p1", S1)
    backfill(client, state, projects)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "assistant", "n": 9}) + "\n")
    report = backfill(client, state, projects)
    assert report.projects["p1"].uploaded == 1
    assert len(api.chunks) == 2  # the first chunk once, the new lines at the next offset


def test_dry_run_uploads_nothing_and_changes_no_state(
    projects: Path, client: CaptureClient, state: StateStore, api: FakeApi, tmp_path: Path
) -> None:
    chat(projects, "p1", S1)
    report = backfill(client, state, projects, dry_run=True)
    assert report.projects["p1"].uploaded == 1  # "would upload"
    assert api.calls == [] and api.attempts == 0
    assert state.known_sessions() == []
    backfill(client, state, projects)
    assert backfill(client, state, projects, dry_run=True).projects["p1"].unchanged == 1


def test_sub_agent_transcripts_and_tool_results_are_uploaded_too(
    projects: Path, client: CaptureClient, state: StateStore, api: FakeApi
) -> None:
    main = chat(projects, "p1", S1)
    agents = main.with_suffix("") / "subagents"
    agents.mkdir(parents=True)
    (agents / "agent-abc123.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    (agents / "agent-abc123.meta.json").write_text('{"agentType":"x"}', encoding="utf-8")
    results = main.with_suffix("") / "tool-results"
    results.mkdir()
    (results / "toolu_1.txt").write_text("output", encoding="utf-8")
    report = backfill(client, state, projects)
    assert report.ok and report.projects["p1"].tool_results == 1
    streams = {stream for stream, _ in api.chunks}
    assert streams == {"main", "agent-abc123", "agent-abc123.meta"}
    assert set(api.tool_results) == {"toolu_1.txt"}


def test_pending_bytes_includes_sub_agent_files(projects: Path, state: StateStore) -> None:
    main = chat(projects, "p1", S1)
    agents = main.with_suffix("") / "subagents"
    agents.mkdir(parents=True)
    sub = agents / "agent-a1.jsonl"
    sub.write_text("12345\n", encoding="utf-8")
    transcript = find_transcripts(projects)[0]
    total = main.stat().st_size + sub.stat().st_size
    assert pending_bytes(state, transcript) == total
    state.set_offset(S1, "main", main.stat().st_size)
    assert pending_bytes(state, transcript) == sub.stat().st_size


def test_an_unreachable_api_stops_the_run_and_reports_it(
    projects: Path, client: CaptureClient, state: StateStore, api: FakeApi
) -> None:
    chat(projects, "p1", S1)
    chat(projects, "p1", S2)
    api.fail_with = ApiUnavailableError("down")
    report = backfill(client, state, projects)
    assert not report.ok and report.stopped is not None
    # The first chat tried its environment record and its chunk (the real client fails fast after the
    # first); the second chat was not even tried.
    assert api.attempts == 2
    api.fail_with = None
    assert backfill(client, state, projects).ok  # nothing was lost: the next run uploads it all
    assert [c for c in api.calls if c[0] == "chunk"] == [("chunk", "main@0")] * 2


def test_backfill_command_prints_a_line_per_project(
    projects: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat(projects, "-home-me-a", S1)
    chat(projects, "-home-me-b", S2)
    monkeypatch.setenv("CCP_INGEST_TOKEN", "x" * 20)
    monkeypatch.setenv("CCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CCP_API_URL", "http://127.0.0.1:1")  # nothing listens: dry run needs none
    args = ["backfill", "--dry-run", "--projects-dir", str(projects)]
    assert cli.main([*args, "--settings-path", str(tmp_path / "none.json")]) == 0
    out = capsys.readouterr().out
    assert "-home-me-a: 1 chat(s), would upload 1, already done 0" in out
    assert "-home-me-b: 1 chat(s), would upload 1" in out


def test_backfill_command_without_chats_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCP_INGEST_TOKEN", "x" * 20)
    monkeypatch.setenv("CCP_STATE_DIR", str(tmp_path / "state"))
    code = cli.main(
        ["backfill", "--projects-dir", str(tmp_path / "no"), "--settings-path", str(tmp_path / "s")]
    )
    assert code == 0 and "no chats found" in capsys.readouterr().out


def test_backfill_command_reads_the_installed_settings_when_the_shell_has_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccp.capture.install import install

    settings = tmp_path / "settings.json"
    install(settings, "http://127.0.0.1:1", "z" * 20, command="x -m ccp.capture hook")
    for name in ("CCP_INGEST_TOKEN", "CCP_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CCP_STATE_DIR", str(tmp_path / "state"))
    code = cli.main(
        ["backfill", "--projects-dir", str(tmp_path / "no"), "--settings-path", str(settings)]
    )
    assert code == 0  # no "CCP_INGEST_TOKEN is required" error
