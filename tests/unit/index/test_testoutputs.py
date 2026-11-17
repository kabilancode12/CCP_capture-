"""Finding the text of a test command's output (inline, or in an uploaded tool-result file)."""

from ccp.index.testoutputs import (
    INLINE,
    MISSING,
    TOOL_RESULT_FILE,
    persisted_file_name,
    read_test_output,
    result_text,
)
from ccp.storage.keys import tool_result_key

SESSION = "chat-1"
NOTE = (
    "<persisted-output>\nOutput too large (31 KB). Full output saved to: "
    "/home/dev/.claude/projects/-work-app/chat-1/tool-results/bx12.txt\n\nPreview (first 2 KB):\n..."
    "</persisted-output>"
)


class FakeStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    def get(self, key: str) -> bytes:
        return self.objects[key]


def _content(text: str) -> list[dict[str, object]]:
    return [{"type": "tool_result", "tool_use_id": "t1", "content": text}]


def test_the_text_of_a_result_is_its_string_or_its_text_blocks() -> None:
    assert result_text(_content("3 passed")) == "3 passed"
    blocks = [{"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
    assert result_text(blocks) == "a\nb"
    assert result_text("plain") == "plain"
    assert result_text(None) == ""


def test_an_inline_output_is_used_as_it_is() -> None:
    text, source = read_test_output(FakeStore({}), SESSION, _content("3 passed in 1s"), None)
    assert (text, source) == ("3 passed in 1s", INLINE)


def test_a_persisted_output_is_read_from_the_uploaded_file() -> None:
    store = FakeStore({tool_result_key(SESSION, "bx12.txt"): b"== 2 failed in 1s =="})
    text, source = read_test_output(store, SESSION, _content(NOTE), None)
    assert (text, source) == ("== 2 failed in 1s ==", TOOL_RESULT_FILE)


def test_the_path_in_the_tool_use_result_names_the_file_too() -> None:
    store = FakeStore({tool_result_key(SESSION, "z9.txt"): b"ok"})
    path = "C:\\Users\\dev\\.claude\\projects\\x\\chat-1\\tool-results\\z9.txt"
    assert read_test_output(store, SESSION, _content("see file"), path) == ("ok", TOOL_RESULT_FILE)


def test_a_file_that_is_not_uploaded_is_missing_and_its_preview_is_not_used() -> None:
    assert read_test_output(FakeStore({}), SESSION, _content(NOTE), None) == (None, MISSING)


def test_a_changed_version_of_the_file_does_not_stand_in_for_the_first() -> None:
    key = tool_result_key(SESSION, "bx12.txt") + "@0123456789abcdef"
    assert read_test_output(FakeStore({key: b"x"}), SESSION, _content(NOTE), None)[1] == MISSING


def test_a_note_without_a_usable_file_name_is_missing() -> None:
    assert persisted_file_name("<persisted-output>no path here</persisted-output>", None) == ""
    assert read_test_output(FakeStore({}), SESSION, _content("<persisted-output>x"), None) == (
        None,
        MISSING,
    )
    assert persisted_file_name("plain output", None) is None
