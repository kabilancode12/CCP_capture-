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


def test_edit_result_shape_is_captured_and_masked() -> None:
    facts = inspect_files([FIXTURE])
    shape = facts.edit_result_shapes["Write"]
    assert "structuredPatch" in shape
    assert shape["filePath"] == "hello.py"  # short value stays visible


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
