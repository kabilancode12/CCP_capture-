from pathlib import Path

import pytest

from ccp.verify.cli import main

FIXTURE = Path(__file__).parent.parent / "fixtures" / "synthetic_session.jsonl"


def test_transcript_command_prints_a_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["transcript", str(FIXTURE)]) == 0
    assert "# Transcript inspection" in capsys.readouterr().out


def test_transcript_command_writes_a_file(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    assert main(["transcript", str(FIXTURE), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# Transcript inspection")


def test_missing_path_returns_error_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["transcript", "no-such-file.jsonl"]) == 2
    assert "Path not found" in capsys.readouterr().err


def test_env_command_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["env"]) == 0
    assert '"surface"' in capsys.readouterr().out
