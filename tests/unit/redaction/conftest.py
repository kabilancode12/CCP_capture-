"""Shared fixtures for the redaction tests.

Fake secrets are assembled from pieces at run time, so no complete fake credential appears
in the source (secret scanners would flag it). Every value here is invented.
"""

import pytest

from ccp.redaction.checksums import verhoeff_check_digit


@pytest.fixture
def fakes() -> dict[str, str]:
    """Return invented sensitive values, keyed by the rule that should catch them."""
    aadhaar_payload = "23456789012"  # first digit 2-9, 11 digits; the 12th is the check digit
    return {
        "aws-access-key-id": "AKIA" + "ABCDEFGHIJKLMNOP",
        "github-token": "ghp_" + "Ab1" * 12,
        "github-fine-grained-token": "github_pat_" + "Ab1Cd2Ef3G" * 3,
        "slack-token": "xoxb-" + "1234567890-abcdefghij",
        "stripe-key": "sk_" + "live_" + "0123456789abcdefABCD",
        "anthropic-api-key": "sk-" + "ant-api03-" + "A" * 24,
        "openai-style-key": "sk-" + "A1b2C3d4" * 5,
        "google-api-key": "AIza" + "Ab1Cd2Ef3G" * 3 + "h4I5j",
        "jwt": "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3" + "." + "SflKxwRJSMeKKF2QT4fw",
        "private-key": (
            "-----BEGIN " + "PRIVATE KEY-----\nMIIBVQIBADANBgkqhki\n-----END " + "PRIVATE KEY-----"
        ),
        "aadhaar": aadhaar_payload + verhoeff_check_digit(aadhaar_payload),
        "pan": "ABC" + "PE1234F",
        "indian-mobile": "9876543210",
        "email": "priya.sharma@example.com",
    }
