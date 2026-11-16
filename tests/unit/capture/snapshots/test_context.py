"""Finding the model and the prompt at the end of a real-shaped transcript."""

import json
from pathlib import Path

from ccp.capture.snapshots.context import read_transcript_context, target_of


def _line(entry: dict[str, object]) -> str:
    return json.dumps(entry) + "\n"


def test_a_prompt_far_behind_very_large_entries_is_still_found(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    huge = {"type": "attachment", "blob": "x" * 2_000_000}  # real transcripts start like this
    prompt = {"type": "user", "uuid": "p1", "message": {"content": "add a retry to the upload"}}
    result = {"type": "user", "uuid": "r1", "message": {"content": [{"type": "tool_result"}]}}
    answer = {"type": "assistant", "message": {"model": "m-1", "content": []}}
    path.write_text(
        "".join(_line(e) for e in (huge, prompt, huge, result, answer, huge)), encoding="utf-8"
    )
    assert read_transcript_context(path) == ("m-1", "p1", "add a retry to the upload")


def test_a_missing_or_empty_transcript_gives_nothing(tmp_path: Path) -> None:
    assert read_transcript_context(None) == (None, None, None)
    assert read_transcript_context(tmp_path / "none.jsonl") == (None, None, None)


def test_a_target_inside_the_repository_is_shown_relative_to_it(tmp_path: Path) -> None:
    inside = {"file_path": str(tmp_path / "src" / "a.py")}
    assert target_of(inside, tmp_path) == "src/a.py"
    assert target_of({"file_path": "/elsewhere/b.py"}, tmp_path) == "/elsewhere/b.py"
    assert target_of({"command": "make   build"}, tmp_path) == "make build"
