"""Recognising test runs in shell commands, and reading how they ended."""

import pytest

from ccp.index.testruns import detect_test_runner, exit_code_of, outcome


@pytest.mark.parametrize(
    ("command", "runner"),
    [
        ("pytest", "pytest"),
        ("pytest -q tests/unit", "pytest"),
        ("python -m pytest -m 'not slow'", "pytest"),
        (".venv/Scripts/python.exe -m pytest tests", "pytest"),
        (r"C:\proj\.venv\Scripts\python.exe -m pytest", "pytest"),
        ("py -m pytest", "pytest"),
        ("uv run pytest -x", "pytest"),
        ("poetry run pytest", "pytest"),
        ("CI=1 pytest", "pytest"),
        ("cd /work/app && pytest -q", "pytest"),
        ("cd app; python3 -m pytest", "pytest"),
        ("set -a; . ./.env; set +a; python -m pytest tests/integration", "pytest"),
        ("python -m unittest discover", "unittest"),
        ("npm test", "npm test"),
        ("npm run test -- --watch=false", "npm test"),
        ("yarn test", "npm test"),
        ("pnpm run test", "npm test"),
        ("npx jest --ci", "jest"),
        ("vitest run", "vitest"),
        ("dotnet test ./src/App.sln", "dotnet test"),
        ("go test ./...", "go test"),
        ("cargo test --release", "cargo test"),
        ("mvn -q clean test", "maven"),
        ("./gradlew --no-daemon test", "gradle"),
        ("./vendor/bin/phpunit", "phpunit"),
        ("bundle exec rspec spec/", "rspec"),
        ("make build\npytest", "pytest"),
    ],
)
def test_a_command_that_starts_a_runner_is_a_test_run(command: str, runner: str) -> None:
    assert detect_test_runner(command) == runner


@pytest.mark.parametrize(
    "command",
    [
        "",
        "ls -la",
        "pip install pytest",
        "git commit -m 'fix pytest config'",
        "grep -rn pytest src",
        "echo 'npm test'",
        "npm install",
        "npm run build",
        "dotnet build",
        "go build ./...",
        "cat pytest.ini",
        "pytest --collect-only",
        "pytest --co -q",
        "pytest --help",
        "python -m pytest --version",
        "rm -rf .pytest_cache",
    ],
)
def test_other_commands_are_not_test_runs(command: str) -> None:
    assert detect_test_runner(command) is None


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Exit code 1\nFAILED tests", 1),
        ("Exit code 127", 127),
        ("Exit code 0", 0),
        ("exit code 1", None),
        ("Error: something else", None),
        ("", None),
        (None, None),
        ("The test said Exit code 3", None),
    ],
)
def test_the_exit_code_is_read_from_the_start_of_a_failure(
    text: str | None, code: int | None
) -> None:
    assert exit_code_of(text) == code


@pytest.mark.parametrize(
    ("is_error", "exit_code", "interrupted", "expected"),
    [
        (False, None, False, (True, 0)),
        (True, 1, False, (False, 1)),
        (True, 5, False, (False, 5)),
        (True, None, False, (None, None)),
        (True, 0, False, (True, 0)),
        (False, None, True, (None, None)),
        (True, 1, True, (None, 1)),
    ],
)
def test_the_outcome_follows_the_error_flag_and_exit_code(
    is_error: bool,
    exit_code: int | None,
    interrupted: bool,
    expected: tuple[bool | None, int | None],
) -> None:
    assert outcome(is_error=is_error, exit_code=exit_code, interrupted=interrupted) == expected


def test_a_request_to_a_local_service_is_a_manual_check_and_a_download_is_not() -> None:
    from ccp.index.testruns import MANUAL_CHECK, detect_test_runner

    assert (
        detect_test_runner("curl -s http://localhost:5000/api/files/parse -T big.csv")
        == MANUAL_CHECK
    )
    assert detect_test_runner("cd app && curl -sf http://127.0.0.1:8080/health") == MANUAL_CHECK
    assert detect_test_runner("Invoke-WebRequest http://localhost:3000") == MANUAL_CHECK
    assert detect_test_runner("curl -O https://example.com/archive.tar.gz") is None
    assert detect_test_runner("echo curl http://localhost:1") is None
    assert detect_test_runner("pytest -q") == "pytest"
