from pathlib import Path

import pytest

from ccp.capture.reader import (
    SourceLine,
    TranscriptError,
    read_complete_lines,
    split_into_chunks,
)


def _file(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_bytes(content)
    return path


def test_complete_lines_are_returned_with_their_sizes(tmp_path: Path) -> None:
    result = read_complete_lines(_file(tmp_path, b'{"a":1}\n{"b":22}\n'), 0)
    assert [(line.text, line.size) for line in result.lines] == [('{"a":1}', 8), ('{"b":22}', 9)]
    assert result.reached_end


def test_a_partial_last_line_waits_for_the_next_read(tmp_path: Path) -> None:
    path = _file(tmp_path, b'{"a":1}\n{"b":')  # Claude Code is still writing the second line
    result = read_complete_lines(path, 0)
    assert [line.text for line in result.lines] == ['{"a":1}']

    path.write_bytes(b'{"a":1}\n{"b":2}\n')  # the line is finished
    resumed = read_complete_lines(path, 8)  # continue after the first line
    assert [line.text for line in resumed.lines] == ['{"b":2}']


def test_reading_from_the_end_gives_nothing(tmp_path: Path) -> None:
    path = _file(tmp_path, b'{"a":1}\n')
    result = read_complete_lines(path, 8)
    assert result.lines == [] and result.reached_end


def test_only_a_partial_line_gives_nothing_yet(tmp_path: Path) -> None:
    result = read_complete_lines(_file(tmp_path, b'{"a":'), 0)
    assert result.lines == [] and result.reached_end


def test_a_full_window_says_more_may_follow(tmp_path: Path) -> None:
    path = _file(tmp_path, b'{"a":1}\n' * 10)  # 80 bytes
    first = read_complete_lines(path, 0, window_bytes=40)
    assert len(first.lines) == 5 and not first.reached_end
    consumed = sum(line.size for line in first.lines)
    assert len(read_complete_lines(path, consumed, window_bytes=100).lines) == 5


def test_offsets_count_bytes_not_characters(tmp_path: Path) -> None:
    line = '{"t":"नमस्ते"}'.encode()
    result = read_complete_lines(_file(tmp_path, line + b"\n"), 0)
    assert result.lines[0].size == len(line) + 1  # more bytes than characters


def test_invalid_utf8_is_replaced_not_fatal(tmp_path: Path) -> None:
    result = read_complete_lines(_file(tmp_path, b'{"a":"\xff"}\n'), 0)
    assert "�" in result.lines[0].text


def test_a_file_shorter_than_the_saved_position_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(TranscriptError, match="shorter"):
        read_complete_lines(_file(tmp_path, b'{"a":1}\n'), 100)


def test_a_line_longer_than_the_window_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(TranscriptError, match="longer than the read window"):
        read_complete_lines(_file(tmp_path, b"x" * 100), 0, window_bytes=50)


def test_a_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_complete_lines(tmp_path / "nope.jsonl", 0)


# ---------------------------------------------------------------- chunking


def _lines(*sizes: int) -> list[SourceLine]:
    return [SourceLine("x" * (size - 1), size) for size in sizes]


def test_lines_are_grouped_up_to_the_limit() -> None:
    chunks = split_into_chunks(_lines(10, 10, 10, 10, 10), max_bytes=25)
    assert [[line.size for line in chunk] for chunk in chunks] == [[10, 10], [10, 10], [10]]


def test_a_line_is_never_split_and_a_big_line_gets_its_own_chunk() -> None:
    chunks = split_into_chunks(_lines(5, 100, 5), max_bytes=20)
    assert [[line.size for line in chunk] for chunk in chunks] == [[5], [100], [5]]


def test_no_lines_give_no_chunks() -> None:
    assert split_into_chunks([], max_bytes=10) == []
