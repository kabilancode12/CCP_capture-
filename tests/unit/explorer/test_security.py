import time

import pytest

from ccp.explorer.security import (
    MIN_PASSWORD_LENGTH,
    PasswordError,
    SessionSigner,
    hash_password,
    verify_password,
)

SECRET = "an-invented-secret-for-tests"  # noqa: S105


def test_a_password_verifies_and_a_wrong_one_does_not() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batterz", stored)


def test_two_hashes_of_one_password_differ_by_salt() -> None:
    assert hash_password("same password here") != hash_password("same password here")


def test_a_short_password_is_refused() -> None:
    with pytest.raises(PasswordError):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))


@pytest.mark.parametrize(
    "stored", [None, "", "plain", "scrypt$1$2", "scrypt$a$b$c$d$e", "md5$1$1$1$a$b"]
)
def test_a_missing_or_unreadable_hash_never_matches(stored: str | None) -> None:
    assert not verify_password("anything at all", stored)


def test_a_cookie_round_trips_and_a_forged_one_is_refused() -> None:
    signer = SessionSigner(SECRET)
    session = signer.new_session(7, "mark", 60)
    cookie = signer.sign(session)
    assert signer.unsign(cookie) == session
    payload, _, signature = cookie.partition(".")
    assert signer.unsign(payload + "." + signature[::-1]) is None
    assert signer.unsign("x" + payload + "." + signature) is None
    assert SessionSigner("another-invented-secret-value").unsign(cookie) is None
    assert signer.unsign(None) is None
    assert signer.unsign("no-dot") is None
    assert signer.unsign("!!!.???") is None


def test_an_expired_cookie_is_refused() -> None:
    signer = SessionSigner(SECRET)
    expired = signer.new_session(1, "m", -5)
    assert signer.unsign(signer.sign(expired)) is None


def test_the_form_token_belongs_to_one_session() -> None:
    signer = SessionSigner(SECRET)
    one, two = signer.new_session(1, "m", 60), signer.new_session(1, "m", 60)
    assert signer.csrf_ok(one, signer.csrf_token(one))
    assert not signer.csrf_ok(one, signer.csrf_token(two))
    assert not signer.csrf_ok(one, "")
    assert not signer.csrf_ok(None, signer.csrf_token(one))


def test_the_password_mark_changes_with_the_password_hash() -> None:
    signer = SessionSigner(SECRET)
    assert signer.password_mark("a") != signer.password_mark("b")
    assert signer.password_mark("a") == signer.password_mark("a")


def test_a_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="CCP_EXPLORER_SECRET"):
        SessionSigner("short")


def test_sessions_expire_after_the_given_seconds() -> None:
    signer = SessionSigner(SECRET)
    assert abs(signer.new_session(None, "", 100).expires_at - (time.time() + 100)) < 5
