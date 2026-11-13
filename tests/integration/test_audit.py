"""``ccp-audit history`` and ``why`` on a real PostgreSQL (see conftest.py).

An invented file is written and then edited three times by the AI, so the lines move and change
owners; every expected answer can be read off the patches below.
"""

import json
from typing import Any

import pytest

from ccp.audit.cli import main
from ccp.audit.repository import AmbiguousPathError, AuditRepository
from ccp.index.repository import IndexRepository
from ccp.index.worker import process_next_job
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

SESSION = "sess-audit"
FILE = "/work/app/src/a.py"


def _chunk(entries: list[dict[str, Any]]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _prompt(uuid: str, parent: str | None, minute: int, text: str, cwd: str = "/work/app") -> Any:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-07-01T10:{minute:02d}:00.000Z",
        "cwd": cwd,
        "message": {"content": text},
    }


def _edit(
    n: int, parent: str, minute: int, result: dict[str, Any], cwd: str = "/work/app"
) -> list[dict[str, Any]]:
    """A tool call and its result, as two entries: ``c<n>`` and ``r<n>``."""
    call_id = f"toolu_{n}"
    return [
        {
            "type": "assistant",
            "uuid": f"c{n}",
            "parentUuid": parent,
            "timestamp": f"2026-07-01T10:{minute:02d}:00.000Z",
            "message": {
                "content": [{"type": "tool_use", "id": call_id, "name": "Edit", "input": {}}]
            },
        },
        {
            "type": "user",
            "uuid": f"r{n}",
            "parentUuid": f"c{n}",
            "timestamp": f"2026-07-01T10:{minute:02d}:30.000Z",
            "cwd": cwd,
            "message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]},
            "toolUseResult": result,
        },
    ]


def _patch(path: str, *hunks: tuple[int, int, int, int, list[str]], **extra: Any) -> dict[str, Any]:
    patch = [
        {"oldStart": a, "oldLines": b, "newStart": c, "newLines": d, "lines": lines}
        for a, b, c, d, lines in hunks
    ]
    return {"filePath": path, "structuredPatch": patch, "userModified": False, **extra}


def _session(session: str = SESSION) -> list[dict[str, Any]]:
    return [
        _prompt("p1", None, 0, "Create the module"),
        *_edit(
            1, "p1", 1,
            {"type": "create", "filePath": FILE, "content": "l1\nl2\nl3\nl4\nl5\n", "structuredPatch": []},
        ),
        _prompt("p2", "r1", 5, "Change the second line and add a header"),
        *_edit(2, "p2", 6, _patch(FILE, (1, 3, 1, 4, ["+header", " l1", "-l2", "+L2", " l3"]))),
        _prompt("p3", "r2", 10, "Drop the fourth line"),
        *_edit(3, "p3", 11, _patch(FILE, (4, 3, 4, 2, [" l3", "-l4", " l5"]), userModified=True)),
    ]  # fmt: skip
    # The file is now: header, l1, L2, l3, l5 -> owners 2, 1, 2, 1, 1


@pytest.fixture
def indexed(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _chunk(_session()))
    PostgresRepository(database).record_chunk(SESSION)
    assert process_next_job(object_store, IndexRepository(database)) is True
    return database


@pytest.fixture
def environment(indexed: DatabaseSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "POSTGRES_HOST": indexed.host,
        "POSTGRES_PORT": str(indexed.port),
        "POSTGRES_DB": indexed.name,
        "POSTGRES_USER": indexed.user,
        "POSTGRES_PASSWORD": indexed.password.reveal(),
    }.items():
        monkeypatch.setenv(name, value)


def test_the_repository_lists_the_edits_oldest_first_with_their_prompts(
    indexed: DatabaseSettings,
) -> None:
    edits = AuditRepository(indexed).file_edits("src/a.py")

    assert [(e.tool, e.lines_added, e.lines_removed, e.user_modified) for e in edits] == [
        ("Edit", 5, 0, None),
        ("Edit", 2, 1, False),
        ("Edit", 0, 1, True),
    ]
    assert [e.prompt for e in edits] == [
        "Create the module",
        "Change the second line and add a header",
        "Drop the fourth line",
    ]


@pytest.mark.parametrize("path", ["src/a.py", FILE, "/work/app/src/a.py"])
def test_a_file_is_found_by_its_relative_or_absolute_path(
    indexed: DatabaseSettings, path: str
) -> None:
    assert len(AuditRepository(indexed).file_edits(path)) == 3


def test_a_file_with_backslashes_in_the_path_is_found(indexed: DatabaseSettings) -> None:
    assert len(AuditRepository(indexed).file_edits("src\\a.py")) == 3


def test_an_unknown_file_has_no_edits(indexed: DatabaseSettings) -> None:
    assert AuditRepository(indexed).file_edits("nothing.py") == []


@pytest.mark.parametrize(
    ("line", "prompt"),
    [
        (1, "Change the second line and add a header"),  # header
        (2, "Create the module"),  # l1
        (3, "Change the second line and add a header"),  # L2
        (4, "Create the module"),  # l3
        (5, "Create the module"),  # l5 (shifted up by the deletion)
    ],
)
def test_why_names_the_edit_that_wrote_each_line(
    environment: None, capsys: pytest.CaptureFixture[str], line: int, prompt: str
) -> None:
    assert main(["why", "src/a.py", str(line)]) == 0

    out = capsys.readouterr().out
    assert f"Line {line} of src/a.py was last written by:" in out
    assert f"prompt: {prompt}" in out


def test_why_says_when_the_developer_changed_the_proposal(
    database: DatabaseSettings,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        _prompt("p1", None, 0, "Add a line"),
        *_edit(1, "p1", 1, _patch(FILE, (1, 1, 1, 2, [" a", "+added"]), userModified=True)),
    ]
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _chunk(entries))
    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))
    for name, value in {
        "POSTGRES_HOST": database.host,
        "POSTGRES_PORT": str(database.port),
        "POSTGRES_DB": database.name,
        "POSTGRES_USER": database.user,
        "POSTGRES_PASSWORD": database.password.reveal(),
    }.items():
        monkeypatch.setenv(name, value)

    assert main(["why", "src/a.py", "2"]) == 0

    assert "changed the AI's proposal before accepting it" in capsys.readouterr().out


def test_why_for_a_line_beyond_the_file_says_no_edit_wrote_it(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["why", "src/a.py", "99"]) == 0

    out = capsys.readouterr().out
    assert "No recorded AI edit wrote line 99" in out
    assert "by a person, or by a shell command" in out


def test_why_for_an_unknown_file(environment: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["why", "nothing.py", "1"]) == 0
    assert "No recorded AI edit of nothing.py." in capsys.readouterr().out


def test_why_rejects_a_line_number_below_one(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["why", "src/a.py", "0"]) == 1
    assert "starts at 1" in capsys.readouterr().err


def test_history_lists_every_edit_with_its_prompt(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["history", "src/a.py"]) == 0

    out = capsys.readouterr().out
    assert "3 recorded edit(s) of src/a.py, oldest first" in out
    assert out.index("Create the module") < out.index("Drop the fourth line")
    assert "(user-modified)" in out
    assert "session sess-aud" in out
    assert "Changes made by a person" in out  # the limits are always stated


def test_history_for_an_unknown_file(environment: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["history", "nothing.py"]) == 0
    assert "No recorded AI edit of nothing.py." in capsys.readouterr().out


def test_a_rewound_edit_is_left_out_unless_asked_for(
    database: DatabaseSettings, object_store: ObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        _prompt("p0", None, 0, "Start"),
        {"type": "assistant", "uuid": "a0", "parentUuid": "p0", "message": {"content": []}},
        _prompt("p1", "a0", 1, "First try"),
        *_edit(1, "p1", 2, _patch(FILE, (1, 1, 1, 1, ["-x", "+y"]))),
        _prompt("p2", "a0", 5, "Second try"),  # a rewind: p1 and its edit are abandoned
    ]
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _chunk(entries))
    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))
    repository = AuditRepository(database)

    assert repository.file_edits("src/a.py") == []
    rewound = repository.file_edits("src/a.py", include_abandoned=True)
    assert [e.is_abandoned for e in rewound] == [True]


def test_the_same_relative_path_in_two_projects_asks_which_one(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    other_file = "/work/other/src/a.py"
    for session, cwd, path in (
        ("s-app", "/work/app", FILE),
        ("s-other", "/work/other", other_file),
    ):
        entries = [
            _prompt("p1", None, 0, "Edit", cwd=cwd),
            *_edit(1, "p1", 1, _patch(path, (1, 1, 1, 1, ["-x", "+y"])), cwd=cwd),
        ]
        object_store.put(transcript_chunk_key(session, "main", 0), _chunk(entries))
        PostgresRepository(database).record_chunk(session)
        process_next_job(object_store, IndexRepository(database))
    repository = AuditRepository(database)

    with pytest.raises(AmbiguousPathError) as caught:
        repository.file_edits("src/a.py")

    assert caught.value.candidates == [FILE, other_file]
    assert len(repository.file_edits(other_file)) == 1  # the full path settles it


def test_the_command_reports_an_ambiguous_path(
    environment: None,
    indexed: DatabaseSettings,
    object_store: ObjectStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        _prompt("p1", None, 0, "Edit", cwd="/work/other"),
        *_edit(
            1,
            "p1",
            1,
            _patch("/work/other/src/a.py", (1, 1, 1, 1, ["-x", "+y"])),
            cwd="/work/other",
        ),
    ]
    object_store.put(transcript_chunk_key("s-other", "main", 0), _chunk(entries))
    PostgresRepository(indexed).record_chunk("s-other")
    process_next_job(object_store, IndexRepository(indexed))

    assert main(["history", "src/a.py"]) == 1
    assert "give the full path of one of" in capsys.readouterr().err
