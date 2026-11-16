"""The capture chain: one labelled commit per change, in one linear history (real Git)."""

from typing import Any

from ccp.capture.snapshots.context import SnapshotContext
from ccp.capture.snapshots.reports import SnapshotReporter
from ccp.capture.state import StateStore

from .conftest import World, git, make_service

SID = "sess-chain"


class FakeApi:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def post_snapshot(self, session_id: str, record: dict[str, Any]) -> None:
        self.records.append({"session_id": session_id, **record})


def _write(world: World, text: str, mode: str = "a") -> None:
    with (world.repo / "parser.py").open(mode, encoding="utf-8") as handle:
        handle.write(text)


def test_every_change_is_one_labelled_commit_and_blame_names_the_cause(world: World) -> None:
    api = FakeApi()
    reporter = SnapshotReporter(StateStore(world.state_dir), api)
    service = make_service(world.state_dir, remote=world.capture, reporter=reporter)
    repo = world.repo

    _write(world, "line one\n", "w")
    ai = SnapshotContext(
        "ai_edit", model="claude-opus-5-5", tool="Write", target="parser.py",
        tool_use_id="toolu_1", prompt_id="p1", prompt_excerpt="write a small parser",
    )  # fmt: skip
    assert "pushed" in (service.snapshot(SID, repo, "PostToolUse", ai) or "")

    _write(world, "line two\n")  # a person adds a line between two prompts
    assert service.snapshot(SID, repo, "UserPromptSubmit", SnapshotContext("human")) is not None

    _write(world, "line three\n")
    shell = SnapshotContext(
        "ai_command",
        model="claude-opus-5-5",
        tool="Bash",
        target="echo >> p",
        tool_use_id="toolu_2",
    )
    service.snapshot(SID, repo, "PostToolUse", shell)

    assert "unchanged" in (service.snapshot(SID, repo, "Stop", SnapshotContext("ai_other")) or "")

    (branch,) = git(
        world.capture, "for-each-ref", "--format=%(refname)", "refs/heads/ccp"
    ).splitlines()
    assert branch.startswith("refs/heads/ccp/") and branch.endswith("/main")
    log = git(world.capture, "log", "--first-parent", "-3", "--format=%an|%s", branch).splitlines()
    assert (
        log[2].startswith("Claude (claude-opus-5-5) for ")
        and "[AI · claude-opus-5-5] Write parser.py" in log[2]
    )
    assert (
        log[1].endswith("|[Human · " + log[1].split("|")[0] + "] Changes made outside Claude")
        or "[Human" in log[1]
    )
    assert "[AI · claude-opus-5-5] Bash" in log[0]

    trailers = git(world.capture, "log", "-1", "--format=%B", branch + "~2")
    for expected in ("CCP-Actor: ai_edit", "CCP-Tool-Use: toolu_1", "CCP-Prompt: p1", f"CCP-Session: {SID}",
                     "Why: write a small parser"):  # fmt: skip
        assert expected in trailers

    # `git blame` on the chain gives the exact cause of every line, whoever made it.
    blame = git(world.capture, "blame", "--line-porcelain", branch, "--", "parser.py")
    commits = [
        line.split()[0]
        for line in blame.splitlines()
        if len(line.split()[0]) == 40 and line[40:41] == " "
    ]
    subjects = [git(world.capture, "log", "-1", "--format=%s", c) for c in commits]
    assert ["[AI" in subjects[0], "[Human" in subjects[1], "Bash" in subjects[2]] == [
        True,
        True,
        True,
    ]

    # Each snapshot was reported to the server with its files and its cause.
    assert [r["actor"] for r in api.records] == ["ai_edit", "human", "ai_command"]
    assert api.records[0]["tool_use_id"] == "toolu_1" and api.records[0]["files"] == [
        {"path": "parser.py", "state": "A"}
    ]
    assert api.records[1]["parent_sha"] == api.records[0]["sha"]  # one linear history
    assert api.records[0]["session_id"] == SID and api.records[0]["chain"].startswith("ccp/")
