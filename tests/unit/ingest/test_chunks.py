import pytest

from ccp.ingest.chunks import ChunkError, validate_chunk


def test_a_chunk_of_complete_json_object_lines_is_accepted() -> None:
    validate_chunk(b'{"a":1}\n{"b":"two"}\n')


def test_unicode_and_line_separator_characters_inside_strings_are_accepted() -> None:
    validate_chunk('{"t":"नमस्ते   inside"}\n'.encode())  # U+2028 is legal inside JSON strings


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"", "empty"),
        (b'{"a":1}', "final newline"),  # incomplete last line
        (b"\xff\xfe\n", "UTF-8"),
        (b'{"a":1}\nnot json\n', "line 2 is not valid JSON"),
        (b"[1,2]\n", "line 1 is not a JSON object"),
        (b'{"a":1}\n\n', "line 2 is not valid JSON"),  # blank line
        (b'"just a string"\n', "line 1 is not a JSON object"),
    ],
)
def test_malformed_chunks_are_rejected_with_a_helpful_message(body: bytes, message: str) -> None:
    with pytest.raises(ChunkError, match=message):
        validate_chunk(body)


def test_error_messages_never_contain_the_content() -> None:
    echoed = "do-not-echo-this"
    with pytest.raises(ChunkError) as error:
        validate_chunk(f'{{"a":1}}\n{echoed}\n'.encode())
    assert echoed not in str(error.value)
