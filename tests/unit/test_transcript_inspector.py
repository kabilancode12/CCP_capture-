from pathlib import Path

from ccp.verify.transcript_inspector import find_transcripts, inspect_files, render_markdown

FIXTURE = Path(__file__).parent.parent / "fixtures" / "synthetic_session.jsonl"


def test_counts_lines_and_invalid_lines() -> None:
    facts = inspect_files([FIXTURE])
    assert facts.line_count == 8
    assert facts.invalid_lines == 1


def test_counts_entry_types() -> None:
    facts = inspect_files([FIXTURE])
    assert facts.entry_types["assistant"] == 3
    assert facts.entry_types["summary"] == 1


def test_assistant_model_and_usage_presence() -> None:
    facts = inspect_files([FIXTURE])
    assert facts.assistant_messages == 3
    assert facts.with_model == 2  # the sub-agent message has no model
    assert facts.with_usage == 2


def test_thinking_variants_are_told_apart() -> None:
    facts = inspect_files([FIXTURE])
    assert facts.thinking_with_text == 1
    assert facts.thinking_signature_only == 1
    assert facts.block_types["redacted_thinking"] == 1


def test_edit_result_shape_keeps_keys_and_hides_every_value() -> None:
    facts = inspect_files([FIXTURE])
    shape = facts.edit_result_shapes["Write"]
    assert "structuredPatch" in shape
    assert shape["filePath"] == "<str len=8>"  # even a short value is hidden


def test_short_secret_in_an_edit_result_never_reaches_the_report(tmp_path: Path) -> None:
    planted_value = "fake-password-123"  # planted test value, not a real secret
    lines = [
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"t1","name":"Edit","input":{}}]}}',
        '{"type":"user","message":{"role":"user","content":'
        '[{"type":"tool_result","tool_use_id":"t1","content":"ok"}]},'
        f'"toolUseResult":{{"newString":"{planted_value}"}}}}',
    ]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(lines), encoding="utf-8")

    report = render_markdown(inspect_files([transcript]))

    assert "newString" in report
    assert planted_value not in report


def test_json_line_that_is_not_an_object_counts_as_invalid(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("[1, 2, 3]\n", encoding="utf-8")
    facts = inspect_files([transcript])
    assert facts.invalid_lines == 1
    assert not facts.entry_types


def test_compact_summary_flag_is_detected(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('{"type":"user","isCompactSummary":true}\n', encoding="utf-8")
    assert inspect_files([transcript]).compaction_markers["isCompactSummary"] == 1


def test_result_of_unknown_tool_is_ignored(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":'
        '[{"type":"tool_result","tool_use_id":"missing","content":"x"}]},"toolUseResult":{"a":1}}\n',
        encoding="utf-8",
    )
    assert inspect_files([transcript]).edit_result_shapes == {}


def test_branch_point_and_sidechain_detected() -> None:
    facts = inspect_files([FIXTURE])
    assert facts.branch_points == 1  # a2 and a3 share the parent u2
    assert facts.sidechain_entries == 1


def test_compaction_marker_detected() -> None:
    facts = inspect_files([FIXTURE])
    assert facts.compaction_markers["subtype=compact_boundary"] == 1


def test_folder_is_searched_for_transcripts(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "agent.jsonl").write_text("{}\n", encoding="utf-8")
    assert [p.name for p in find_transcripts([tmp_path])] == ["agent.jsonl"]


def test_report_contains_the_main_sections() -> None:
    report = render_markdown(inspect_files([FIXTURE]))
    for heading in ("## Entry types", "## Thinking", "## Conversation tree"):
        assert heading in report
