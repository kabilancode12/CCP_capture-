import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ccp.catalog import documents
from ccp.catalog.loader import ChatData

NOW = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def _data() -> ChatData:
    chat = {
        "chat_id": "c1", "title": "Fix", "account_id": 1, "account_uuid": "u", "account_label": "claude1",
        "account_name": "Real Name", "project_id": 2, "project_key": "github.com/a/b",
        "project_name": "b", "project_label": None, "git_remote": "github.com/a/b",
        "folder": "/work/b", "branch": "main", "start_commit": "abc", "surface": "cli",
        "status": "ended", "started_at": NOW, "ended_at": None, "last_activity_at": NOW,
    }  # fmt: skip

    def usage(model: str | None, effort: str | None, responses: int, tokens: int) -> dict[str, Any]:
        return {
            "model": model, "effort": effort, "requested_model": None, "responses": responses,
            "input_tokens": Decimal(tokens), "output_tokens": Decimal(1), "thinking_tokens": None,
            "cache_read_tokens": None, "cache_creation_tokens": Decimal(2), "thinking_ms": None,
        }  # fmt: skip

    def event(
        kind: str, content: Any, actor: str = "ai", abandoned: bool = False
    ) -> dict[str, Any]:
        return {"event_type": kind, "actor": actor, "is_abandoned": abandoned, "content": content}

    return ChatData(
        chat=chat,
        events=[
            event("prompt", "a", "human"),
            event("prompt", "b", "human", abandoned=True),
            event("response", [{"type": "text", "text": "x"}]),
            event("tool_call", [{"type": "tool_use", "name": "Edit"}, {"type": "tool_use", "name": "Bash"}]),
        ],
        usage_by_model=[usage("m1", "high", 2, 10), usage("m1", None, 1, 5), usage("<synthetic>", None, 1, 99)],
        cost_by_model=[{"model": "m2", "cost_usd": Decimal("1.5")}],
        cost_total={"total_cost_usd": Decimal("1.5"), "has_unknown_model_cost": False},
        test_runs=[
            {"test_run_id": 1, "passed": False, "tests_passed": 1, "tests_failed": 2, "tests_skipped": None,
             "raw_object_key": "sessions/c1/main/0.jsonl", "raw_line_number": 4, "command": "pytest"},
            {"test_run_id": 2, "passed": True, "tests_passed": 3, "tests_failed": 0, "tests_skipped": 1,
             "raw_object_key": None, "raw_line_number": None, "command": "pytest"},
        ],
        test_cases=[{"test_run_id": 1, "position": 1, "name": "test_a", "outcome": "failed", "message": "boom"}],
        file_changes=[
            {"path": "a.py", "file_role": "code", "lines_added": 3, "lines_removed": 1, "tests_added": None,
             "raw_object_key": "k", "raw_line_number": 2},
            {"path": "test_a.py", "file_role": "test", "lines_added": 5, "lines_removed": 0, "tests_added": 2,
             "raw_object_key": None, "raw_line_number": None},
            {"path": "a.py", "file_role": None, "lines_added": 1, "lines_removed": 1, "tests_added": None,
             "raw_object_key": None, "raw_line_number": None},
        ],
        decisions=[{"status": "approved"}, {"status": "rejected"}],
        agents=[{"agent_id": "a1", "instructions": "x" * 5000, "result": "short"}],
        corrections=[
            {"correction_id": 1, "kind": "test_fix", "corrected_by": "test", "detected_by": "rule", "note": None,
             "session_seq": 1, "trigger_at": NOW, "trigger_key": "k", "trigger_line": 4, "fix_at": None,
             "fix_key": None, "fix_line": None}
        ],
    )  # fmt: skip


def test_to_json_is_pretty_utf8_and_handles_database_types() -> None:
    raw = documents.to_json(
        {"b": Decimal("1.5"), "a": Decimal(3), "t": NOW, "s": "é"}, sort_keys=True
    )
    text = raw.decode("utf-8")
    assert text.endswith("}\n") and '  "a": 3,' in text and "é" in text
    assert json.loads(text) == {"a": 3, "b": 1.5, "s": "é", "t": NOW.isoformat()}
    assert text.index('"a"') < text.index('"b"')


def test_summary_has_ids_models_tokens_cost_and_counts() -> None:
    summary = documents.build_summary(_data(), "catalog/accounts/x")
    assert summary["chat_id"] == "c1" and summary["catalog_path"] == "catalog/accounts/x"
    assert summary["account"] == {
        "account_id": 1, "account_uuid": "u", "label": "claude1", "name": "Real Name"
    }  # fmt: skip
    assert summary["models"] == ["m1", "m2"]  # the placeholder model is not listed
    assert summary["tokens"] == {
        "input": 15, "output": 2, "cache_read": 0, "cache_creation": 4, "total": 21
    }  # fmt: skip
    assert summary["cost_usd"] == Decimal("1.5")
    counts = summary["counts"]
    assert counts["prompts"] == 1  # the rewound prompt is not counted
    assert counts["tool_calls"] == 2 and counts["replies"] == 1
    assert counts["decisions"] == 1  # a rejected decision is not counted
    assert counts["tests_passed"] == 4 and counts["tests_failed"] == 2
    assert counts["file_changes"] == 3 and counts["files_changed"] == 2
    assert "email" not in json.dumps(summary, default=str)


def test_tests_document_nests_cases_in_their_run() -> None:
    tests = documents.build_tests(_data())
    assert tests["summary"] == {
        "runs": 2, "runs_passed": 1, "runs_failed": 1,
        "tests_passed": 4, "tests_failed": 2, "tests_skipped": 1,
    }  # fmt: skip
    first, second = tests["runs"]
    assert first["cases"] == [
        {"position": 1, "name": "test_a", "outcome": "failed", "message": "boom"}
    ]
    assert first["raw"] == {"key": "sessions/c1/main/0.jsonl", "line": 4}
    assert second["cases"] == [] and second["raw"] is None


def test_code_changes_summarise_roles_and_lines() -> None:
    changes = documents.build_code_changes(_data())
    assert changes["summary"] == {
        "changes": 3, "files": 2, "lines_added": 9, "lines_removed": 2, "tests_added": 2,
        "changes_by_role": {"code": 1, "test": 1, "unknown": 1},
    }  # fmt: skip
    assert changes["changes"][0]["raw"] == {"key": "k", "line": 2}
    assert "raw_object_key" not in changes["changes"][0]


def test_corrections_carry_raw_references() -> None:
    (correction,) = documents.build_corrections(_data())
    assert correction["trigger"] == {"key": "k", "line": 4} and correction["fix"] is None


def test_subagent_text_is_cut_and_usage_counts_effort_per_response() -> None:
    data = _data()
    agent = documents.build_subagents(data)["agents"][0]
    assert len(agent["instructions"]) == documents.TEXT_MAX + 3 and agent["result"] == "short"
    usage = documents.build_usage(data)
    assert usage["responses_by_effort"] == {"high": 2, "unknown": 2}
    assert usage["total"]["cost_usd"] == Decimal("1.5")


def test_readme_names_every_file() -> None:
    text = documents.build_readme(_data(), "2026-07-01__fix__c1", 2)
    for name in documents.FILES:
        assert f"`{name}`" in text
    assert "claude1" in text and "`c1`" in text
    assert "ccp-catalog build" in documents.TOP_README
