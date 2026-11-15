"""The HTTP client, tested against a small local server that can be told how to answer."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ccp.capture.api import ApiRejectedError, ApiUnavailableError, HttpApiClient
from ccp.redaction.records import RedactionRecord
from ccp.settings import Secret

TOKEN = "unit" + "-test-token-value"  # invented; built from pieces for the secret scanner


class _Script:
    """What the test server should answer, and what it received."""

    status = 200
    body: Any = {"stored": True}
    location: str | None = None  # for redirects
    raw: bytes | None = None  # sent as is instead of ``body`` (for answers that are not JSON)
    received: list[dict[str, Any]]

    def __init__(self) -> None:
        self.received = []


def _handler(script: _Script) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            script.received.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "type": self.headers.get("Content-Type"),
                    "body": self.rfile.read(length),
                }
            )
            self.send_response(script.status)
            if script.location:
                self.send_header("Location", script.location)
            payload = script.raw if script.raw is not None else json.dumps(script.body).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_PUT = do_POST = _answer  # noqa: N815

        def log_message(self, *args: Any) -> None:  # keep test output quiet
            pass

    return Handler


@pytest.fixture
def script() -> _Script:
    return _Script()


@pytest.fixture
def client(script: _Script) -> Iterator[HttpApiClient]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(script))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield HttpApiClient(f"http://127.0.0.1:{server.server_port}", Secret(TOKEN), 5.0)
    finally:
        server.shutdown()
        server.server_close()


def test_a_chunk_is_sent_with_the_token_the_right_path_and_content_type(
    client: HttpApiClient, script: _Script
) -> None:
    script.status, script.body = 201, {"stored": True}
    assert client.put_chunk("s1", "agent-a", 4096, b'{"a":1}\n') is True

    (request,) = script.received
    assert request["method"] == "PUT"
    assert request["path"] == "/v1/sessions/s1/streams/agent-a/chunks/4096"
    assert request["auth"] == f"Bearer {TOKEN}"
    assert request["type"] == "application/x-ndjson"
    assert request["body"] == b'{"a":1}\n'


def test_stored_false_means_it_was_already_there(client: HttpApiClient, script: _Script) -> None:
    script.status, script.body = 200, {"stored": False}
    assert client.put_chunk("s1", "main", 0, b"{}\n") is False
    assert client.put_attachment("a" * 64, b"{}") is False


def test_an_attachment_is_sent_to_its_digest_url(client: HttpApiClient, script: _Script) -> None:
    client.put_attachment("b" * 64, b'{"k":"v"}')
    assert script.received[0]["path"] == f"/v1/attachments/{'b' * 64}"
    assert script.received[0]["type"] == "application/json"


def test_redactions_are_sent_as_a_json_report(client: HttpApiClient, script: _Script) -> None:
    script.status, script.body = 200, {"recorded": 1}
    client.post_redactions("s1", [RedactionRecord("email", "key:1:a", 2)])

    (request,) = script.received
    assert (request["method"], request["path"]) == ("POST", "/v1/sessions/s1/redactions")
    assert json.loads(request["body"]) == {
        "records": [{"rule_id": "email", "location": "key:1:a", "occurrences": 2}]
    }


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_server_errors_mean_try_again_later(
    client: HttpApiClient, script: _Script, status: int
) -> None:
    script.status, script.body = status, {"error": "busy"}
    with pytest.raises(ApiUnavailableError):
        client.put_chunk("s1", "main", 0, b"{}\n")


@pytest.mark.parametrize("status", [400, 401, 409, 413, 422])
def test_client_errors_mean_the_request_was_refused(
    client: HttpApiClient, script: _Script, status: int
) -> None:
    script.status, script.body = status, {"error": "no thanks"}
    with pytest.raises(ApiRejectedError) as error:
        client.put_chunk("s1", "main", 0, b"{}\n")
    assert error.value.status == status
    assert "no thanks" in str(error.value)  # the server's message is kept


def test_a_redirect_is_refused_so_the_token_cannot_be_forwarded(
    client: HttpApiClient, script: _Script
) -> None:
    script.status, script.location = 307, "http://127.0.0.1:1/elsewhere"
    with pytest.raises(ApiRejectedError):
        client.put_chunk("s1", "main", 0, b"{}\n")
    assert len(script.received) == 1  # the redirect target was never contacted


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>Please log in to the guest network</html>",  # a captive portal
        b"\xff\xfe not even text",
        b"",  # an empty body: no "stored" flag
        b"[]",  # JSON, but not an object
        b'{"ok":true}',  # an object without the "stored" flag
    ],
)
def test_a_success_answer_that_is_not_the_apis_means_try_again_later(
    client: HttpApiClient, script: _Script, raw: bytes
) -> None:
    script.status, script.raw = 200, raw
    with pytest.raises(ApiUnavailableError, match="not from the API"):
        client.put_chunk("s1", "main", 0, b"{}\n")
    with pytest.raises(ApiUnavailableError, match="not from the API"):
        client.put_attachment("c" * 64, b"{}")


def test_a_report_with_a_non_json_answer_means_try_again_later(
    client: HttpApiClient, script: _Script
) -> None:
    script.status, script.raw = 200, b"<html>proxy</html>"
    with pytest.raises(ApiUnavailableError):
        client.post_redactions("s1", [RedactionRecord("email", "key:1:a", 1)])


def test_an_unreachable_server_means_try_again_later() -> None:
    client = HttpApiClient("http://127.0.0.1:1", Secret(TOKEN), 2.0)  # nothing listens on port 1
    with pytest.raises(ApiUnavailableError, match="cannot reach"):
        client.put_chunk("s1", "main", 0, b"{}\n")


def test_errors_never_contain_the_token(client: HttpApiClient, script: _Script) -> None:
    script.status, script.body = 401, {"error": "bad token"}
    with pytest.raises(ApiRejectedError) as rejected:
        client.put_chunk("s1", "main", 0, b"{}\n")
    unreachable = HttpApiClient("http://127.0.0.1:1", Secret(TOKEN), 2.0)
    with pytest.raises(ApiUnavailableError) as unavailable:
        unreachable.put_chunk("s1", "main", 0, b"{}\n")
    assert TOKEN not in str(rejected.value) and TOKEN not in str(unavailable.value)
