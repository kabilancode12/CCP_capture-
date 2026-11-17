import json

from ccp.catalog.transcripts import read_lines, transcript_streams
from ccp.storage.attachments import externalise_line


class FakeStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def put_if_absent(self, key: str, data: bytes) -> bool:
        return self.objects.setdefault(key, data) is data


def test_streams_are_grouped_in_order_without_meta_and_tool_results() -> None:
    store = FakeStore(
        {
            "sessions/c/main/000000000010.jsonl": b"{}\n",
            "sessions/c/main/000000000000.jsonl": b"{}\n",
            "sessions/c/agent-a1/000000000000.jsonl": b"{}\n",
            "sessions/c/agent-a1.meta/000000000000.jsonl": b"{}\n",
            "sessions/c/tool-results/out.txt": b"text",
            "sessions/other/main/000000000000.jsonl": b"{}\n",
        }
    )
    streams = transcript_streams(store, "c")
    assert streams == {
        "main": ["sessions/c/main/000000000000.jsonl", "sessions/c/main/000000000010.jsonl"],
        "agent-a1": ["sessions/c/agent-a1/000000000000.jsonl"],
    }


def test_one_item_per_line_in_order_and_unreadable_lines_are_kept_as_markers() -> None:
    store = FakeStore(
        {
            "k1": b'{"n": 1}\n{"n": 2}\n',
            "k2": b'{"n": 3}\nnot json\n\xff\xfe\n"text"\n',
        }
    )
    items = read_lines(store, ["k1", "k2"])
    assert items[:3] == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert items[3] == {"type": "ccp-unreadable-line", "object": "k2", "line": 2}
    assert items[4] == {"type": "ccp-unreadable-line", "object": "k2", "line": 3}
    assert items[5] == "text"


def test_externalised_attachment_values_are_put_back_and_missing_ones_stay_pointers() -> None:
    store = FakeStore({})
    line = json.dumps(
        {
            "type": "attachment",
            "uuid": "u",
            "attachment": {"type": "skill_listing", "content": "x" * 3000},
        },
        separators=(",", ":"),
    )
    pointer_line = externalise_line(line, store).text
    assert "$ccp_ref" in pointer_line
    store.objects["k"] = (pointer_line + "\n").encode()
    assert read_lines(store, ["k"])[0]["attachment"]["content"] == "x" * 3000

    for key in [k for k in store.objects if k.startswith("attachments/")]:
        del store.objects[key]  # the value is gone
    kept = read_lines(_Missing(store.objects), ["k"])[0]
    assert "$ccp_ref" in json.dumps(kept)


class _Missing(FakeStore):
    def get(self, key: str) -> bytes:
        if key.startswith("attachments/"):
            from ccp.storage import ObjectNotFoundError

            raise ObjectNotFoundError("gone")
        return super().get(key)
