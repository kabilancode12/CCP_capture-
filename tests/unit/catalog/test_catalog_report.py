"""The single-file report: readable, complete, and safe for odd content."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ccp.catalog import documents
from ccp.catalog.loader import ChatData
from ccp.catalog.report import demote, fence, fmt_time, render_report

T0 = datetime(2026, 11, 16, 5, 8, 3, tzinfo=UTC)
T1 = datetime(2026, 11, 16, 5, 20, 0, tzinfo=UTC)
KEY = "sessions/c1/main/000000000000.jsonl"


def _event(
    kind: str, content: Any, at: datetime, actor: str = "ai", **extra: Any
) -> dict[str, Any]:
    return {"event_id": extra.pop("event_id", 0), "event_type": kind, "actor": actor,
            "occurred_at": at, "is_abandoned": False, "content": content, "compact": None, **extra}  # fmt: skip


def _data() -> ChatData:
    chat = {
        "chat_id": "c1", "title": "Parser | demo", "account_id": 1, "account_uuid": "uuid-1",
        "account_label": None, "account_name": "dev", "project_id": 2, "project_key": "key-2",
        "project_name": "proj", "project_label": None, "git_remote": None, "folder": "/work/proj",
        "branch": "HEAD", "start_commit": None, "surface": "vscode", "status": "closed",
        "started_at": T0, "ended_at": T1, "last_activity_at": T1,
    }  # fmt: skip
    events = [
        _event("prompt", "Build a parser\nwith ``` fences ```", T0, "human"),
        _event("thinking", [{"type": "thinking", "thinking": "I weigh two options."}], T0),
        _event("response", [{"type": "text", "text": "# Plan\n\n## Step\n```\n# not a heading\n```"}], T0),
        _event("tool_call", [{"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": "ls\nwc -l", "description": "list"}}], T0),
        _event("tool_result", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                "content": [{"type": "text", "text": "boom | pipe"}, {"type": "image"}]}], T0),
        _event("prompt", "Second ask", T1, "human"),
    ]  # fmt: skip
    return ChatData(
        chat=chat,
        events=events,
        sessions=[
            {
                "seq": 1,
                "start_source": "startup",
                "started_at": T0,
                "ended_at": T1,
                "end_reason": "other",
                "start_commit": None,
                "end_commit": None,
                "reported_by_client": True,
            }
        ],  # fmt: skip
        decisions=[
            {
                "decision_id": 7,
                "seq": 1,
                "title": "Use a stream",
                "goal": "Parse big files",
                "options": ["stream: low memory", "load: simple"],
                "chosen": "stream",
                "reason": "memory",
                "result": "worked",
                "tests_summary": "none",
                "decided_by": "claude",
                "files": [{"path": "P.cs", "change": "created", "via": "shell"}],
                "thinking": [{"text": "note one", "key": KEY, "line": 3}],
                "llm_usage": {"write": {"model": "m"}},
                "commit_shas": ["abc123"],
                "evidence_lines": [{"key": KEY, "line": 5}],
                "status": "approved",
                "reviewed_by_kind": "ai",
                "reviewer": "reviewer-model",
                "review_note": "fine",
                "review_history": [
                    {
                        "at": "t",
                        "by_kind": "ai",
                        "reviewer": "r",
                        "status": "approved",
                        "note": "ok",
                    }
                ],
                "model": "writer-model",
                "prompt_version": 2,
                "created_at": T1,
                "updated_at": T1,
            }
        ],  # fmt: skip
        test_runs=[
            {
                "test_run_id": 1,
                "command": "pytest",
                "framework": "pytest",
                "passed": False,
                "exit_code": 1,
                "tests_passed": 2,
                "tests_failed": 1,
                "tests_skipped": 0,
                "output_source": "result",
                "occurred_at": T0,
                "raw_object_key": KEY,
                "raw_line_number": 9,
            }
        ],  # fmt: skip
        test_cases=[
            {
                "test_run_id": 1,
                "position": 1,
                "name": "test_a",
                "outcome": "failed",
                "message": "bad",
            }
        ],
        file_changes=[
            {
                "file_change_id": 1,
                "path": "src/a.py",
                "tool": "Edit",
                "change_type": "update",
                "actor_class": "ai_edit",
                "user_modified": False,
                "lines_added": 3,
                "lines_removed": 1,
                "file_role": "code",
                "tests_added": 0,
                "occurred_at": T0,
                "is_abandoned": False,
                "raw_object_key": KEY,
                "raw_line_number": 4,
            }
        ],  # fmt: skip
        commits=[
            {
                "commit_sha": "abc123",
                "method": "line_match",
                "confidence": Decimal("0.8"),
                "matched_lines": 4,
                "total_lines": 5,
                "committed_at": T1,
            }
        ],  # fmt: skip
        corrections=[
            {
                "correction_id": 3,
                "kind": "test_fix",
                "corrected_by": "test",
                "detected_by": "rule",
                "note": "fixed",
                "session_seq": 1,
                "trigger_at": T0,
                "trigger_key": KEY,
                "trigger_line": 2,
                "fix_at": T1,
                "fix_key": KEY,
                "fix_line": 8,
            }
        ],  # fmt: skip
        agents=[
            {
                "agent_id": "a1",
                "agent_type": "coder",
                "description": "d",
                "model": "m",
                "effort": None,
                "spawn_depth": 1,
                "tool_use_id": "t9",
                "workflow_run_id": None,
                "workflow_name": None,
                "started_at": T0,
                "ended_at": T1,
                "event_count": 5,
                "instructions": "x" * 5000,
                "result": "done",
            }
        ],  # fmt: skip
        slash_commands=[
            {
                "event_id": 0,
                "occurred_at": T0,
                "is_abandoned": False,
                "name": "/model",
                "arguments": "opus",
            }
        ],
        usage_by_model=[
            {
                "model": "m1",
                "effort": "high",
                "requested_model": None,
                "responses": 2,
                "input_tokens": Decimal(10),
                "output_tokens": Decimal(20),
                "thinking_tokens": Decimal(5),
                "cache_read_tokens": Decimal(7),
                "cache_creation_tokens": Decimal(8),
                "thinking_ms": Decimal(99),
            }
        ],  # fmt: skip
        cost_by_model=[
            {
                "model": "m1",
                "input_tokens": 1,
                "output_tokens": 2,
                "thinking_tokens": 3,
                "cache_read_tokens": 4,
                "cache_creation_tokens": 5,
                "web_search_requests": 6,
                "cost_usd": Decimal("1.5"),
            },
            {
                "model": "helper",
                "input_tokens": 1,
                "output_tokens": 1,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "cost_usd": Decimal("0.01"),
            },
        ],  # fmt: skip
        shadow_commits=[
            {
                "sha": "d" * 40,
                "parent_sha": None,
                "seq": 1,
                "actor": "ai_command",
                "developer": "dev@x.org",
                "model": "m1",
                "tool": "Bash",
                "target": "make build",
                "tool_use_id": "toolu_9",
                "prompt_uuid": "p1",
                "prompt_excerpt": "build it",
                "agent": "main",
                "event": "PostToolUse",
                "chain": "ccp/dev/host/main",
                "captured_at": T1,
                "files": [{"path": "out.bin", "state": "A"}],
                "files_total": 1,
            }
        ],
        cost_total={"total_cost_usd": Decimal("1.51"), "has_unknown_model_cost": False},
    )


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    if isinstance(value, list | tuple):
        return [leaf for v in value for leaf in _leaves(v)]
    return [] if value is None else [value]


def test_report_has_every_section_and_a_title() -> None:
    text = render_report(_data(), "catalog/x", 1)
    assert text.startswith("# Parser | demo\n")
    for number in range(1, 11):
        assert f"\n## {number}. " in text
    assert "### Conversation 1 — 05:08:03" in text and "### Conversation 2 — 05:20:00" in text


def test_every_value_of_every_catalog_document_is_in_the_report() -> None:
    data = _data()
    text = render_report(data, "catalog/x", 1)
    docs = [
        documents.build_summary(data, "catalog/x"), documents.build_sessions(data),
        documents.build_decisions(data), documents.build_tests(data),
        documents.build_code_changes(data), documents.build_corrections(data),
        documents.build_usage(data),
    ]  # fmt: skip
    missing = []
    for leaf in _leaves(docs):
        if isinstance(leaf, datetime):
            wanted = fmt_time(leaf)
        elif isinstance(leaf, Decimal):
            wanted = str(int(leaf)) if leaf == leaf.to_integral_value() else str(leaf)
        elif isinstance(leaf, bool):
            wanted = "yes" if leaf else "no"
        else:
            wanted = str(leaf)
        if wanted not in text and wanted.replace("|", "\\|") not in text:
            missing.append(wanted)
    assert missing == []


def test_sub_agent_text_is_not_cut_and_helper_model_is_named() -> None:
    text = render_report(_data(), "catalog/x", 1)
    assert "x" * 5000 in text
    assert "helper (internal helper)" in text and "no Git repository" in text


def test_replies_cannot_outrank_the_turn_and_fences_protect_headings() -> None:
    text = render_report(_data(), "catalog/x", 0)
    assert "\n##### Plan\n" in text and "\n###### Step\n" in text
    assert "\n# not a heading\n" in text  # inside a fence: untouched
    assert demote("# a\n```\n# b\n```") == "##### a\n```\n# b\n```"


def test_tool_input_and_error_output_are_kept_in_full_and_tables_survive_pipes() -> None:
    text = render_report(_data(), "catalog/x", 0)
    assert "ls\nwc -l" in text and "boom | pipe" in text and "[image block]" in text
    assert "Result of action 1 (Bash): error" in text
    assert "| Title | Parser \\| demo |" in text


def test_fence_is_longer_than_backticks_inside() -> None:
    assert fence("a ``` b")[0] == "````"


def test_empty_chat_says_none_recorded_in_each_section() -> None:
    data = _data()
    empty = ChatData(chat=data.chat)
    text = render_report(empty, "catalog/x", 0)
    assert text.count("*None recorded.*") >= 8


def test_a_sub_agents_conversation_and_shell_commands_are_in_full() -> None:
    data = _data()
    data.agent_events = [
        {**_event("prompt", "find the slow query", T0, "subagent"), "stream": "agent-a1"},
        {**_event("tool_call", [{"type": "tool_use", "id": "s1", "name": "Bash",
                                 "input": {"command": "grep -rn SELECT src/"}}], T0), "stream": "agent-a1"},
        {**_event("tool_result", [{"type": "tool_result", "tool_use_id": "s1", "content": "src/db.py:12: SELECT *"}], T0),
         "stream": "agent-a1"},
    ]  # fmt: skip
    text = render_report(data, "catalog/x", 1)
    assert "Full conversation of this sub-agent (3 entries)" in text
    assert "Instructions to the sub-agent" in text and "grep -rn SELECT src/" in text
    assert "src/db.py:12: SELECT *" in text


def test_every_other_transcript_entry_is_in_the_last_section() -> None:
    raw = [{"type": "user", "message": {"content": "hi"}},
           {"type": "api-request-blob", "text": "cd /work && make build"},
           {"type": "ai-title", "aiTitle": "Parser work"}]  # fmt: skip
    text = render_report(_data(), "catalog/x", 0, raw)
    assert "## 11. Everything else Claude Code recorded" in text
    assert "cd /work && make build" in text and "Parser work" in text
    assert "| api-request-blob | 1 |" in text
