import pytest

from ccp.redaction.checksums import is_valid_verhoeff, verhoeff_check_digit


def test_known_verhoeff_example() -> None:
    # The classic worked example: payload 236 has check digit 3.
    assert verhoeff_check_digit("236") == "3"
    assert is_valid_verhoeff("2363")


def test_a_wrong_check_digit_is_rejected() -> None:
    assert not is_valid_verhoeff("2364")


@pytest.mark.parametrize("payload", ["1", "123456789", "98765432101", "00000000000"])
def test_generated_check_digit_always_validates(payload: str) -> None:
    assert is_valid_verhoeff(payload + verhoeff_check_digit(payload))


def test_single_digit_change_is_always_detected() -> None:
    number = "234567890124"
    good = number[:-1] + verhoeff_check_digit(number[:-1])
    for index in range(len(good)):
        changed = good[:index] + str((int(good[index]) + 1) % 10) + good[index + 1 :]
        assert not is_valid_verhoeff(changed)


def test_non_digits_are_rejected() -> None:
    assert not is_valid_verhoeff("12a4")
    assert not is_valid_verhoeff("")
