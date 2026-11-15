"""Reading the output of a test run: how many tests passed, failed and were skipped, and which.

A test run (``test_run``) knows its runner (``pytest``, ``go test`` ...) and how it ended. This
module
reads the *text* the runner printed and returns a :class:`TestSummary`: the three counts and one
:class:`TestCaseResult` per test that the output names. Supported: pytest, unittest, jest, vitest,
go test, cargo test and dotnet test. For any other runner (Maven, Gradle ...) no summary is made.

Rules that apply to every parser:

* **Counts come from the runner's own summary line** when it prints one (``2 failed, 5 passed in
  0.3s``), because that is exact even when the per-test lines are not printed. A count the output
  does not state is ``None``, never a guess (``go test`` without ``-v`` does not list passed tests).
* **Cases** are the tests the output names with their outcome. A runner without ``-v`` names the
  failures only; that is what the output holds. At most :data:`MAX_CASES` are kept (failures first,
  then skipped, then passed, each in the order printed) and a message is cut to
  :data:`MAX_MESSAGE` characters.
* The text is already redacted (it comes from the stored transcript). Messages are the first line
  of the failure text, never the whole traceback.
* A text with no recognisable summary gives ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

MAX_CASES = 500
MAX_MESSAGE = 300

PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OUTCOME_ORDER = {FAILED: 0, ERROR: 0, SKIPPED: 1, PASSED: 2}


@dataclass(frozen=True)
class TestCaseResult:
    """One test named in the output."""

    __test__ = False  # not a pytest test class, whatever its name suggests

    name: str
    outcome: str  # passed | failed | error | skipped
    message: str | None = None


@dataclass(frozen=True)
class TestSummary:
    """What a test run's output says.

    Attributes:
        passed: Tests that passed, or ``None`` if the output does not say.
        failed: Tests that failed or had an error, or ``None``.
        skipped: Tests that were skipped, or ``None``.
        cases: The tests named in the output (capped, see the module description).
    """

    __test__ = False

    passed: int | None
    failed: int | None
    skipped: int | None
    cases: tuple[TestCaseResult, ...] = field(default_factory=tuple)


def parse_test_output(framework: str, text: str) -> TestSummary | None:
    """Read a test run's output.

    Args:
        framework: The runner as ``test_run.framework`` names it (``pytest``, ``npm test`` ...).
            A script such as ``npm test`` can run any runner, so every parser is tried.
        text: The output the command printed.

    Returns:
        The summary, or ``None`` if the output holds nothing this module recognises.
    """
    clean = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    for parser in _parsers_for(framework):
        summary = parser(clean)
        if summary is not None:
            return _limited(summary)
    return None


def _limited(summary: TestSummary) -> TestSummary:
    """Cut the messages and keep the most telling cases when there are too many."""
    cases = [
        TestCaseResult(case.name[:500], case.outcome, _short(case.message))
        for case in summary.cases
    ]
    cases.sort(key=lambda case: _OUTCOME_ORDER[case.outcome])  # stable: printed order is kept
    return TestSummary(summary.passed, summary.failed, summary.skipped, tuple(cases[:MAX_CASES]))


def _short(message: str | None) -> str | None:
    """The first non-empty line of a message, cut to :data:`MAX_MESSAGE` characters."""
    for line in (message or "").splitlines():
        if line.strip():
            return line.strip()[:MAX_MESSAGE]
    return None


def _first_text_after(lines: list[str], start: int, limit: int = 6) -> str | None:
    """The first non-empty line within ``limit`` lines after line ``start``."""
    for line in lines[start + 1 : start + 1 + limit]:
        if line.strip():
            return line.strip()
    return None


# ---------------------------------------------------------------------------------------- pytest

_PYTEST_SUMMARY = re.compile(
    r"^[=\s]*((?:\d+ [a-z]+(?:\s*\([^)]*\))?(?:, )?)+)\s+in\s+[\d.:]+s?(?:\s*\([\d:]+\))?\s*[=\s]*$"
)
_PYTEST_NO_TESTS = re.compile(r"^[=\s]*no tests ran\b")
_PYTEST_COUNT = re.compile(
    r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)"
)
_PYTEST_VERBOSE = re.compile(
    r"^(\S+?::\S.*?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b(?:\s+\(.*?\))?(?:\s+\[\s*\d+%\])?\s*$"
)
_PYTEST_SHORT = re.compile(
    r"^(FAILED|ERROR|PASSED|SKIPPED|XFAIL|XPASS)\s+(\S+?::\S+?)(?:\s+-\s+(.*))?$"
)
_PYTEST_OUTCOME = {
    "PASSED": PASSED,
    "XPASS": PASSED,
    "FAILED": FAILED,
    "ERROR": ERROR,
    "SKIPPED": SKIPPED,
    "XFAIL": SKIPPED,
}


def _parse_pytest(text: str) -> TestSummary | None:
    counts: dict[str, int] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if _PYTEST_NO_TESTS.match(stripped):
            counts = {}
        elif _PYTEST_SUMMARY.match(stripped):
            counts = {}
            for number, word in _PYTEST_COUNT.findall(stripped):
                key = word.rstrip("s") if word.startswith(("error", "warning")) else word
                counts[key] = counts.get(key, 0) + int(number)
    if counts is None:
        return None
    # Cases: the short summary (names the failures, with a message) and -v / -rA lines. A name seen
    # twice keeps the entry that has a message.
    found: dict[str, TestCaseResult] = {}
    for line in text.splitlines():
        stripped = line.strip()
        short = _PYTEST_SHORT.match(stripped)
        verbose = _PYTEST_VERBOSE.match(stripped)
        if short:
            name, outcome, message = short[2], _PYTEST_OUTCOME[short[1]], short[3]
        elif verbose:
            name, outcome, message = verbose[1], _PYTEST_OUTCOME[verbose[2]], None
        else:
            continue
        old = found.get(name)
        if old is None or (message and not old.message):
            found[name] = TestCaseResult(name, outcome, message)
    return TestSummary(
        passed=counts.get("passed", 0) + counts.get("xpassed", 0),
        failed=counts.get("failed", 0) + counts.get("error", 0),
        skipped=counts.get("skipped", 0),
        cases=tuple(found.values()),
    )


# ------------------------------------------------------------------------------------- unittest

_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*$")
_UNITTEST_RESULT = re.compile(r"^(OK|FAILED)\s*(?:\((.*)\))?\s*$")
_UNITTEST_FAILURE = re.compile(r"^(FAIL|ERROR): (\S.*)$")
_UNITTEST_VERBOSE = re.compile(r"^(\S.*?) \.\.\. (ok|FAIL|ERROR|skipped\b.*|expected failure)$")


def _parse_unittest(text: str) -> TestSummary | None:
    lines = text.splitlines()
    ran = None
    details: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = _UNITTEST_RAN.match(line.strip())
        if match is None:
            continue
        ran = int(match[1])
        result = next(
            (
                m
                for m in (_UNITTEST_RESULT.match(x.strip()) for x in lines[index + 1 : index + 4])
                if m
            ),
            None,
        )
        details = dict(re.findall(r"(\w+)=(\d+)", result[2] or "")) if result else {}
    if ran is None:
        return None
    failed = int(details.get("failures", 0)) + int(details.get("errors", 0))
    skipped = int(details.get("skipped", 0))
    cases: dict[str, TestCaseResult] = {}
    for index, line in enumerate(lines):
        failure = _UNITTEST_FAILURE.match(line.strip())
        verbose = _UNITTEST_VERBOSE.match(line.strip())
        if failure and lines[index + 1 : index + 2] and set(lines[index + 1].strip()) == {"-"}:
            outcome = FAILED if failure[1] == "FAIL" else ERROR
            cases[failure[2]] = TestCaseResult(failure[2], outcome, _unittest_message(lines, index))
        elif verbose and verbose[1] not in cases:
            word = verbose[2]
            outcome = PASSED if word in ("ok", "expected failure") else SKIPPED
            if word in ("FAIL", "ERROR"):
                outcome = FAILED if word == "FAIL" else ERROR
            cases[verbose[1]] = TestCaseResult(verbose[1], outcome)
    return TestSummary(ran - failed - skipped, failed, skipped, tuple(cases.values()))


def _unittest_message(lines: list[str], start: int) -> str | None:
    """The last line of a unittest failure block, which names the exception."""
    block: list[str] = []
    for line in lines[start + 2 : start + 60]:
        if line.startswith("=" * 10) or line.startswith("Ran "):
            break
        if line.strip() and not line.startswith("-" * 10):
            block.append(line.strip())
    return block[-1] if block else None


# ------------------------------------------------------------------------------- jest / vitest

_JEST_TOTALS = re.compile(r"^\s*Tests:\s+(.*?)\s*$")
_VITEST_TOTALS = re.compile(r"^\s*Tests\s+(\d.*?)\s*$")
_JS_COUNT = re.compile(r"(\d+)\s+(failed|passed|skipped|todo|pending)")
_JS_CASE = re.compile(r"^\s*(✓|✔|√|✕|✗|×|○|↓|-)\s+(\S.*?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$")
_JS_FAILURE = re.compile(r"^\s*●\s+(\S.*?)\s*$")
_VITEST_FAIL = re.compile(r"^\s*FAIL\s+(\S.* > \S.*?)\s*$")
_JS_MARK = {"✓": PASSED, "✔": PASSED, "√": PASSED, "✕": FAILED, "✗": FAILED, "×": FAILED}


def _parse_jest(text: str) -> TestSummary | None:
    lines = text.splitlines()
    counts: dict[str, int] | None = None
    for line in lines:
        match = _JEST_TOTALS.match(line) or _VITEST_TOTALS.match(line)
        if match and _JS_COUNT.search(match[1]):
            counts = {}
            for number, word in _JS_COUNT.findall(match[1]):
                counts[word] = counts.get(word, 0) + int(number)
    if counts is None:
        return None
    messages: dict[str, str | None] = {}
    for index, line in enumerate(lines):
        failure = _JS_FAILURE.match(line) or _VITEST_FAIL.match(line)
        if failure and not failure[1].startswith(("Console", "Test suite failed")):
            messages.setdefault(failure[1], _first_text_after(lines, index))
    cases: dict[str, TestCaseResult] = {}
    for line in lines:
        case = _JS_CASE.match(line)
        if case is None or case[1] == "-" and not case[2].startswith(("skipped", "todo")):
            continue
        outcome = _JS_MARK.get(case[1], SKIPPED)
        name = case[2]
        message = _message_for(messages, name) if outcome == FAILED else None
        cases.setdefault(name, TestCaseResult(name, outcome, message))
    if not any(c.outcome == FAILED for c in cases.values()):
        for name, message in messages.items():  # no -v: the failures only
            cases.setdefault(name, TestCaseResult(name, FAILED, message))
    return TestSummary(
        passed=counts.get("passed", 0),
        failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0) + counts.get("todo", 0) + counts.get("pending", 0),
        cases=tuple(cases.values()),
    )


def _message_for(messages: dict[str, str | None], name: str) -> str | None:
    """The failure message of a test: jest names it ``Suite › test``, the case line ``test``."""
    for full, message in messages.items():
        if full == name or full.endswith((f" › {name}", f" > {name}")):
            return message
    return None


# ------------------------------------------------------------------------------------- go test

_GO_CASE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+) \([\d.]+s\)\s*$")
_GO_RUN = re.compile(r"^\s*=== (?:RUN|PAUSE|CONT)\s")
_GO_PACKAGE = re.compile(r"^(?:ok|FAIL)\s+\S+\s")
_GO_OUTCOME = {"PASS": PASSED, "FAIL": FAILED, "SKIP": SKIPPED}


def _parse_go(text: str) -> TestSummary | None:
    lines = text.splitlines()
    cases: dict[str, TestCaseResult] = {}
    verbose = False
    package_seen = False
    for index, line in enumerate(lines):
        verbose = verbose or bool(_GO_RUN.match(line))
        package_seen = package_seen or bool(_GO_PACKAGE.match(line))
        match = _GO_CASE.match(line)
        if match is None:
            continue
        outcome = _GO_OUTCOME[match[1]]
        message = _go_message(lines, index) if outcome == FAILED else None
        cases.setdefault(match[2], TestCaseResult(match[2], outcome, message))
    if not cases:
        return None
    outcomes = [case.outcome for case in cases.values()]
    passes = outcomes.count(PASSED)
    # A run without -v prints the failures only: passed and skipped are then unknown.
    known = verbose or passes > 0 or outcomes.count(SKIPPED) > 0
    return TestSummary(
        passed=passes if known else None,
        failed=outcomes.count(FAILED),
        skipped=outcomes.count(SKIPPED) if known else None,
        cases=tuple(cases.values()),
    )


def _go_message(lines: list[str], index: int) -> str | None:
    """The indented output line next to a ``--- FAIL`` line: after it (no -v) or before it (-v)."""
    for neighbour in (index + 1, index - 1):
        if 0 <= neighbour < len(lines):
            line = lines[neighbour]
            if line.startswith((" ", "\t")) and not line.strip().startswith(("---", "===")):
                return line.strip()
    return None


# ---------------------------------------------------------------------------------- cargo test

_CARGO_RESULT = re.compile(
    r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored;"
)
_CARGO_CASE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
_CARGO_FAILURE = re.compile(r"^---- (\S+) stdout ----$")
_CARGO_OUTCOME = {"ok": PASSED, "FAILED": FAILED, "ignored": SKIPPED}


def _parse_cargo(text: str) -> TestSummary | None:
    lines = text.splitlines()
    totals = [0, 0, 0]
    results = 0
    cases: dict[str, TestCaseResult] = {}
    messages: dict[str, str | None] = {}
    for index, line in enumerate(lines):
        result = _CARGO_RESULT.match(line)
        case = _CARGO_CASE.match(line)
        failure = _CARGO_FAILURE.match(line)
        if result:
            results += 1
            totals = [a + int(b) for a, b in zip(totals, result.groups(), strict=True)]
        elif case:
            cases.setdefault(case[1], TestCaseResult(case[1], _CARGO_OUTCOME[case[2]]))
        elif failure:
            messages[failure[1]] = _cargo_panic_message(lines, index)
    if results == 0:
        return None
    named = tuple(
        TestCaseResult(c.name, c.outcome, messages.get(c.name)) if c.outcome == FAILED else c
        for c in cases.values()
    )
    return TestSummary(totals[0], totals[1], totals[2], named)


def _cargo_panic_message(lines: list[str], start: int) -> str | None:
    """The panic message under a ``---- name stdout ----`` heading (old or new format)."""
    for index in range(start + 1, min(start + 12, len(lines))):
        line = lines[index].strip()
        if "panicked at" in line:
            tail = line.split("panicked at", 1)[1].strip()
            if tail.endswith(":"):  # new format: the message is on the next line
                return _first_text_after(lines, index)
            return tail
    return None


# ---------------------------------------------------------------------------------- dotnet test

_DOTNET_TOTALS = re.compile(
    r"^(?:Passed|Failed)!\s+-\s+Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+)"
)
_DOTNET_OLD = re.compile(r"^\s*(Total tests|Passed|Failed|Skipped):\s*(\d+)\s*$")
_DOTNET_CASE = re.compile(r"^\s+(Passed|Failed|Skipped)\s+(\S.*?)(?:\s+\[[^\]]*\])?\s*$")
_DOTNET_OUTCOME = {"Passed": PASSED, "Failed": FAILED, "Skipped": SKIPPED}


def _parse_dotnet(text: str) -> TestSummary | None:
    lines = text.splitlines()
    totals = [0, 0, 0]  # passed, failed, skipped
    found = False
    old: dict[str, int] = {}
    for line in lines:
        match = _DOTNET_TOTALS.match(line.strip())
        legacy = _DOTNET_OLD.match(line)
        if match:
            found = True
            failed, passed, skipped = (int(x) for x in match.groups())
            totals = [totals[0] + passed, totals[1] + failed, totals[2] + skipped]
        elif legacy:
            old[legacy[1]] = old.get(legacy[1], 0) + int(legacy[2])
    if not found and "Total tests" in old:
        found = True
        totals = [old.get("Passed", 0), old.get("Failed", 0), old.get("Skipped", 0)]
    if not found:
        return None
    cases: dict[str, TestCaseResult] = {}
    for index, line in enumerate(lines):
        case = _DOTNET_CASE.match(line)
        if case is None:
            continue
        outcome = _DOTNET_OUTCOME[case[1]]
        message = _dotnet_message(lines, index) if outcome == FAILED else None
        cases.setdefault(case[2], TestCaseResult(case[2], outcome, message))
    return TestSummary(totals[0], totals[1], totals[2], tuple(cases.values()))


def _dotnet_message(lines: list[str], start: int) -> str | None:
    """The line after ``Error Message:`` under a failed test."""
    for index in range(start + 1, min(start + 6, len(lines))):
        if lines[index].strip() == "Error Message:":
            return _first_text_after(lines, index, 3)
    return None


# ------------------------------------------------------------------------------------- choosing

Parser = Callable[[str], TestSummary | None]

_BY_FRAMEWORK: dict[str, tuple[Parser, ...]] = {
    "pytest": (_parse_pytest,),
    "unittest": (_parse_unittest,),
    "jest": (_parse_jest,),
    "vitest": (_parse_jest,),
    "go test": (_parse_go,),
    "cargo test": (_parse_cargo,),
    "dotnet test": (_parse_dotnet,),
}


def _parsers_for(framework: str) -> tuple[Parser, ...]:
    """The parsers to try: the runner's own, or all of them for a script such as ``npm test``."""
    if framework in _BY_FRAMEWORK:
        return _BY_FRAMEWORK[framework]
    if framework == "npm test":
        return (_parse_jest, _parse_pytest, _parse_go, _parse_cargo, _parse_dotnet)
    return ()
