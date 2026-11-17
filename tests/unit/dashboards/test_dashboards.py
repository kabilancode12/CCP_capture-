"""The dashboard definitions, the Metabase client and the setup steps (with a fake Metabase)."""

import json
import re
from typing import Any

import pytest

from ccp.dashboards.client import MetabaseClient, MetabaseError
from ccp.dashboards.definitions import CARDS, COLLECTION_NAME, DASHBOARD_NAME
from ccp.dashboards.setup import (
    ensure_cards,
    ensure_collection,
    ensure_dashboard,
    ensure_database,
    run_setup,
    sign_in,
    wait_for_tables,
)
from ccp.db.readers import DASHBOARD_VIEWS
from ccp.settings import DashboardSettings, Secret

SETTINGS = DashboardSettings(
    metabase_url="http://metabase.test",
    admin_email="admin@example.com",
    admin_password=Secret("admin-password-1"),
    database_name="ccp",
    database_host="postgres",
    database_port=5432,
    reader_user="ccp_reader",
    reader_password=Secret("reader-password-1"),
)


class FakeMetabase:
    """Answers requests from a table of canned answers and remembers every request."""

    def __init__(self, answers: dict[tuple[str, str], Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str, Any, str | None]] = []
        self.status: dict[tuple[str, str], int] = {}

    def __call__(self, method: str, url: str, body: Any, session: str | None) -> tuple[int, Any]:
        path = url.removeprefix("http://metabase.test")
        self.calls.append((method, path, body, session))
        key = (method, path)
        if key not in self.answers and method == "POST" and path.startswith("/api/card"):
            return 200, {"id": 100 + len(self.calls)}
        return self.status.get(key, 200), self.answers.get(key, {})

    def sent(self, method: str, path: str) -> list[Any]:
        return [body for m, p, body, _ in self.calls if (m, p) == (method, path)]


def _client(fake: FakeMetabase) -> MetabaseClient:
    return MetabaseClient("http://metabase.test/", fake)


# ------------------------------------------------------------------------ the definitions


def test_card_names_are_unique() -> None:
    names = [card.name for card in CARDS]
    assert len(names) == len(set(names))


def test_every_card_reads_only_the_dashboard_views() -> None:
    for card in CARDS:
        tables = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", card.sql, re.IGNORECASE)
        assert tables, card.name
        assert set(tables) <= set(DASHBOARD_VIEWS), (card.name, tables)


def test_every_card_is_a_read_only_select() -> None:
    for card in CARDS:
        assert card.sql.lstrip().upper().startswith("SELECT"), card.name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|GRANT)\b", card.sql, re.I)


def test_the_layout_fits_the_grid_and_no_cards_overlap() -> None:
    taken: set[tuple[int, int]] = set()
    for card in CARDS:
        assert card.col >= 0 and card.row >= 0
        assert card.col + card.width <= 24, card.name
        cells = {
            (row, col)
            for row in range(card.row, card.row + card.height)
            for col in range(card.col, card.col + card.width)
        }
        assert not cells & taken, f"{card.name} overlaps another card"
        taken |= cells


def test_displays_are_ones_metabase_knows() -> None:
    assert {card.display for card in CARDS} <= {
        "scalar",
        "bar",
        "line",
        "pie",
        "table",
        "row",
        "area",
    }


# ----------------------------------------------------------------------------- the client


def test_a_request_goes_to_the_address_with_the_session() -> None:
    fake = FakeMetabase({("GET", "/api/x"): {"ok": True}})
    client = _client(fake)
    client.use_session("abc")

    assert client.request("GET", "/api/x") == {"ok": True}
    assert fake.calls == [("GET", "/api/x", None, "abc")]


def test_signing_in_keeps_the_session_for_later_requests() -> None:
    fake = FakeMetabase({("POST", "/api/session"): {"id": "the-session"}})
    client = _client(fake)

    client.login("a@example.com", "secret-password-1")
    client.request("GET", "/api/y")

    assert fake.sent("POST", "/api/session") == [
        {"username": "a@example.com", "password": "secret-password-1"}
    ]
    assert fake.calls[-1][3] == "the-session"


def test_a_sign_in_without_a_session_is_an_error() -> None:
    with pytest.raises(MetabaseError, match="without a session"):
        _client(FakeMetabase({("POST", "/api/session"): {}})).login("a@example.com", "pw")


def test_an_error_status_names_the_request_and_is_cut_short() -> None:
    fake = FakeMetabase({("GET", "/api/z"): {"message": "x" * 1000}})
    fake.status[("GET", "/api/z")] = 403

    with pytest.raises(MetabaseError) as caught:
        _client(fake).request("GET", "/api/z")

    assert "GET /api/z answered 403" in str(caught.value)
    assert len(str(caught.value)) < 400


def test_an_unreachable_metabase_is_reported_without_a_traceback() -> None:
    client = MetabaseClient("http://127.0.0.1:1")  # nothing listens on port 1

    with pytest.raises(MetabaseError, match="cannot reach Metabase"):
        client.request("GET", "/api/health")


# ------------------------------------------------------------------------------- setup steps


def test_the_first_time_setup_creates_the_administrator() -> None:
    fake = FakeMetabase(
        {
            ("GET", "/api/session/properties"): {"setup-token": "tok", "has-user-setup": False},
            ("POST", "/api/setup"): {"id": "fresh-session"},
        }
    )
    client = _client(fake)

    assert sign_in(client, SETTINGS) is True

    (body,) = fake.sent("POST", "/api/setup")
    assert body["token"] == "tok"  # noqa: S105  (a made-up setup token)
    assert body["user"]["email"] == "admin@example.com"
    assert body["prefs"]["allow_tracking"] is False
    client.request("GET", "/api/after")
    assert fake.calls[-1][3] == "fresh-session"


def test_when_the_setup_was_done_before_it_signs_in_even_though_the_token_is_still_offered() -> (
    None
):
    fake = FakeMetabase(
        {
            ("GET", "/api/session/properties"): {"setup-token": "tok", "has-user-setup": True},
            ("POST", "/api/session"): {"id": "s"},
        }
    )

    assert sign_in(_client(fake), SETTINGS) is False
    assert fake.sent("POST", "/api/setup") == []
    assert fake.sent("POST", "/api/session") == [
        {"username": "admin@example.com", "password": "admin-password-1"}
    ]


def test_without_a_token_it_signs_in() -> None:
    fake = FakeMetabase(
        {("GET", "/api/session/properties"): {}, ("POST", "/api/session"): {"id": "s"}}
    )

    assert sign_in(_client(fake), SETTINGS) is False


def test_the_database_is_added_with_the_read_only_role() -> None:
    fake = FakeMetabase(
        {("GET", "/api/database"): {"data": []}, ("POST", "/api/database"): {"id": 7}}
    )

    assert ensure_database(_client(fake), SETTINGS) == 7

    (body,) = fake.sent("POST", "/api/database")
    assert body["engine"] == "postgres"
    assert body["details"] == {
        "host": "postgres",
        "port": 5432,
        "dbname": "ccp",
        "user": "ccp_reader",
        "password": "reader-password-1",
        "ssl": False,
    }


def test_an_existing_database_gets_its_connection_updated_not_duplicated() -> None:
    fake = FakeMetabase({("GET", "/api/database"): {"data": [{"id": 3, "name": "CCP"}]}})

    assert ensure_database(_client(fake), SETTINGS) == 3

    assert fake.sent("POST", "/api/database") == []
    assert fake.sent("PUT", "/api/database/3")[0]["details"]["user"] == "ccp_reader"


def test_it_waits_until_the_views_have_been_read() -> None:
    answers = iter(
        [{"tables": []}, {"tables": [{"name": "other"}]}, {"tables": [{"name": "d_session"}]}]
    )

    class Sequence(FakeMetabase):
        def __call__(
            self, method: str, url: str, body: Any, session: str | None
        ) -> tuple[int, Any]:
            if url.endswith("/metadata"):
                return 200, next(answers)
            return super().__call__(method, url, body, session)

    waits: list[float] = []
    wait_for_tables(_client(Sequence()), 5, sleep=waits.append, clock=lambda: 0.0)

    assert len(waits) == 2


def test_it_gives_up_when_the_views_never_appear() -> None:
    fake = FakeMetabase({("GET", "/api/database/5/metadata"): {"tables": []}})
    ticks = iter(range(0, 1000, 40))

    with pytest.raises(MetabaseError, match="dashboard views"):
        wait_for_tables(_client(fake), 5, sleep=lambda _: None, clock=lambda: float(next(ticks)))


def test_the_collection_is_found_or_made() -> None:
    existing = FakeMetabase({("GET", "/api/collection"): [{"id": 4, "name": COLLECTION_NAME}]})
    assert ensure_collection(_client(existing)) == 4
    assert existing.sent("POST", "/api/collection") == []

    missing = FakeMetabase(
        {
            ("GET", "/api/collection"): [{"id": 1, "name": "Other"}],
            ("POST", "/api/collection"): {"id": 9},
        }
    )
    assert ensure_collection(_client(missing)) == 9


def test_every_card_is_made_as_a_native_question_in_the_collection() -> None:
    fake = FakeMetabase({("GET", "/api/card"): []})

    ids = ensure_cards(_client(fake), database_id=7, collection_id=4)

    assert set(ids) == {card.name for card in CARDS}
    bodies = fake.sent("POST", "/api/card")
    assert len(bodies) == len(CARDS)
    first = bodies[0]
    assert first["dataset_query"]["type"] == "native"
    assert first["dataset_query"]["database"] == 7
    assert first["collection_id"] == 4
    assert first["dataset_query"]["native"]["query"] == CARDS[0].sql


def test_existing_cards_are_updated_and_cards_of_other_collections_are_left_alone() -> None:
    fake = FakeMetabase(
        {
            ("GET", "/api/card"): [
                {"id": 11, "name": CARDS[0].name, "collection_id": 4},
                {"id": 12, "name": CARDS[1].name, "collection_id": 99},  # somebody else's
            ]
        }
    )

    ids = ensure_cards(_client(fake), 7, 4)

    assert ids[CARDS[0].name] == 11
    assert len(fake.sent("PUT", "/api/card/11")) == 1
    assert len(fake.sent("POST", "/api/card")) == len(CARDS) - 1
    assert ids[CARDS[1].name] != 12


def test_the_dashboard_holds_exactly_the_layout() -> None:
    fake = FakeMetabase({("GET", "/api/dashboard"): [], ("POST", "/api/dashboard"): {"id": 6}})
    card_ids = {card.name: 200 + index for index, card in enumerate(CARDS)}

    assert ensure_dashboard(_client(fake), 4, card_ids) == 6

    (created,) = fake.sent("POST", "/api/dashboard")
    assert created["name"] == DASHBOARD_NAME
    (updated,) = fake.sent("PUT", "/api/dashboard/6")
    placed = updated["dashcards"]
    assert [p["card_id"] for p in placed] == [card_ids[c.name] for c in CARDS]
    assert all(p["id"] < 0 for p in placed)
    assert (placed[0]["row"], placed[0]["col"], placed[0]["size_x"]) == (0, 0, 6)


def test_an_existing_dashboard_is_updated_not_duplicated() -> None:
    fake = FakeMetabase(
        {("GET", "/api/dashboard"): [{"id": 8, "name": DASHBOARD_NAME, "collection_id": 4}]}
    )

    assert ensure_dashboard(_client(fake), 4, {c.name: 1 for c in CARDS}) == 8
    assert fake.sent("POST", "/api/dashboard") == []


def test_a_dashboard_of_the_same_name_elsewhere_is_not_ours() -> None:
    fake = FakeMetabase(
        {
            ("GET", "/api/dashboard"): [{"id": 8, "name": DASHBOARD_NAME, "collection_id": 99}],
            ("POST", "/api/dashboard"): {"id": 9},
        }
    )

    assert ensure_dashboard(_client(fake), 4, {c.name: 1 for c in CARDS}) == 9


def test_the_whole_setup_runs_in_order_and_returns_the_dashboard_address() -> None:
    fake = FakeMetabase(
        {
            ("GET", "/api/session/properties"): {"has-user-setup": True, "setup-token": "t"},
            ("POST", "/api/session"): {"id": "s"},
            ("GET", "/api/database"): {"data": [{"id": 3, "name": "CCP"}]},
            ("GET", "/api/database/3/metadata"): {"tables": [{"name": "d_session"}]},
            ("GET", "/api/collection"): [{"id": 4, "name": COLLECTION_NAME}],
            ("GET", "/api/card"): [],
            ("GET", "/api/dashboard"): [],
            ("POST", "/api/dashboard"): {"id": 6},
        }
    )

    address = run_setup(_client(fake), SETTINGS)

    assert address == "http://metabase.test/dashboard/6"
    paths = [path for _, path, _, _ in fake.calls]
    assert paths.index("/api/session") < paths.index("/api/database/3/sync_schema")
    assert paths.index("/api/database/3/sync_schema") < paths.index("/api/card")
    assert paths.index("/api/card") < paths.index("/api/dashboard/6")


def test_no_password_appears_in_an_error_message() -> None:
    fake = FakeMetabase({("POST", "/api/session"): {"message": "Password did not match"}})
    fake.status[("POST", "/api/session")] = 401

    with pytest.raises(MetabaseError) as caught:
        _client(fake).login("admin@example.com", "admin-password-1")

    assert "admin-password-1" not in str(caught.value)
    assert json.dumps(str(caught.value)).count("admin-password-1") == 0
