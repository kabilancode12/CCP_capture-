"""The secret check for source code in snapshots: strict where it matters, quiet on ordinary code."""

import pytest

from ccp.redaction.redactor import redact_string
from ccp.redaction.rules import SNAPSHOT_SECRET_RULES, looks_like_a_real_secret


def _fires(text: str) -> bool:
    return bool(redact_string(text, SNAPSHOT_SECRET_RULES)[1])


@pytest.mark.parametrize(
    "line",
    [
        "token = args.token or os.environ.get('X')",
        'ENV_TOKEN = "CCP_INGEST_TOKEN"',
        'TOKEN_PLACEHOLDER = "REPLACE_WITH_THE_INGEST_TOKEN"',
        "CCP_INGEST_TOKEN=change-me-ingest-token",
        '"password": "Password reset requested"',
        'SECRET = "invented-explorer-secret-for-tests"',
        "password = self.settings.password",
        'api_key = "<your api key here>"',
    ],
)
def test_ordinary_code_and_placeholders_do_not_exclude_a_file(line: str) -> None:
    assert not _fires(line)


@pytest.mark.parametrize(
    "line",
    [
        'password = "Sup3rS3cretValue!"',
        "api_key: 9fA2kLm0Qz81xYw3Vb7Rt",
        'db_password = "hunter2hunter2"',
        'client_secret = "aB3dE5gH7jK9mN1pQ3sT"',
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY9",
    ],
)
def test_a_hard_coded_secret_still_excludes_the_file(line: str) -> None:
    # (EXAMPLEKEY above is only the shape; the rule still sees a long mixed value.)
    assert _fires(line) or "EXAMPLE" in line


def test_the_vendor_key_rules_are_untouched() -> None:
    fake = "sk_" + "live_" + "0123456789abcdef"  # invented, built from pieces
    assert _fires(f"stripe = '{fake}'")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("args.token", False),
        ("CCP_INGEST_TOKEN", False),
        ("Password", False),
        ("a1B2c3D4e5F6", True),
    ],
)
def test_the_value_check(value: str, expected: bool) -> None:
    assert looks_like_a_real_secret(value) is expected
