from ccp.verify.masking import mask_value, shape_of


def test_short_string_is_shown() -> None:
    assert mask_value("hello") == "hello"


def test_long_string_is_replaced_by_its_length() -> None:
    assert mask_value("x" * 41) == "<str len=41>"


def test_non_string_values_show_their_type() -> None:
    assert mask_value(42) == "<int>"
    assert mask_value(None) == "null"


def test_shape_keeps_keys_and_hides_long_values() -> None:
    shape = shape_of({"name": "ok", "secret": "s" * 50, "items": [1, 2, 3]})
    assert shape == {"name": "ok", "secret": "<str len=50>", "items": ["<list len=3>", "<int>"]}


def test_shape_can_hide_every_string() -> None:
    assert shape_of({"email": "a@b.co"}, max_visible=0) == {"email": "<str len=6>"}


def test_shape_stops_at_the_depth_limit() -> None:
    assert shape_of({"a": {"b": {"c": 1}}}, depth=2) == {"a": {"b": "<dict>"}}
