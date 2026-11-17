"""Reading test output: one realistic sample per runner (the numbers are invented)."""

from ccp.index.testparsers import MAX_CASES, MAX_MESSAGE, TestCaseResult, parse_test_output

PYTEST_FAILING = """\
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.3.1
collected 12 items

tests/test_a.py ....F.                                                    [ 50%]
tests/test_b.py ..s.E.                                                    [100%]

=================================== FAILURES ===================================
_______________________________ test_login_fails _______________________________
E   assert 401 == 200
=========================== short test summary info ============================
FAILED tests/test_a.py::test_login_fails - assert 401 == 200
ERROR tests/test_b.py::test_fixture_broken - RuntimeError: no database
=== 1 failed, 8 passed, 1 skipped, 1 warning, 1 error in 0.52s ===
"""

PYTEST_QUIET_OK = (
    "........                                                 [100%]\n8 passed in 0.12s\n"
)

PYTEST_VERBOSE = """\
tests/test_a.py::test_one PASSED                                         [ 33%]
tests/test_a.py::test_two FAILED                                         [ 66%]
tests/test_a.py::test_three SKIPPED (not on linux)                       [100%]
FAILED tests/test_a.py::test_two - AssertionError: nope
\x1b[31m1 failed\x1b[0m, 1 passed, 1 skipped in 1.00s
"""

JEST = """\
PASS src/a.test.js
FAIL src/b.test.js
  math
    ✓ adds (3 ms)
    ✕ subtracts (5 ms)
    ○ skipped multiplies

  ● math › subtracts

    expect(received).toBe(expected) // Object.is equality

    Expected: 1

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 1 skipped, 1 passed, 3 total
Snapshots:   0 total
"""

JEST_QUIET_FAILURE = """\
FAIL src/b.test.js
  ● calculator › divides by zero

    expect(received).toThrow()

Tests:       1 failed, 4 passed, 5 total
"""

VITEST = """\
 ✓ src/a.test.ts > adds 2ms
 × src/a.test.ts > subtracts 3ms
 FAIL  src/a.test.ts > subtracts
AssertionError: expected 1 to be 2

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 5 passed | 2 skipped (8)
"""

GO_VERBOSE = """\
=== RUN   TestAdd
--- PASS: TestAdd (0.00s)
=== RUN   TestDivide
    calc_test.go:21: want 2, got 0
--- FAIL: TestDivide (0.00s)
=== RUN   TestSkip
--- SKIP: TestSkip (0.00s)
FAIL
FAIL	example.com/calc	0.004s
"""

GO_QUIET_FAILURE = """\
--- FAIL: TestDivide (0.00s)
    calc_test.go:21: want 2, got 0
FAIL
FAIL	example.com/calc	0.004s
"""

GO_QUIET_OK = "ok  \texample.com/calc\t0.004s\n"

CARGO = """\
running 3 tests
test tests::adds ... ok
test tests::divides ... FAILED
test tests::slow ... ignored

failures:

---- tests::divides stdout ----
thread 'tests::divides' panicked at src/lib.rs:12:9:
assertion `left == right` failed

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

running 1 test
test src/lib.rs - add (line 3) ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.10s
"""

DOTNET = """\
  Failed Calc.Tests.DivideTests.ByZero [12 ms]
  Error Message:
   System.DivideByZeroException : Attempted to divide by zero.
  Stack Trace:
     at Calc.Divide()

Failed!  - Failed:     1, Passed:     4, Skipped:     1, Total:     6, Duration: 20 ms - Calc.Tests.dll (net8.0)
"""

DOTNET_OLD = """\
Test Run Failed.
Total tests: 5
     Passed: 3
     Failed: 2
 Total time: 1.2 Seconds
"""

UNITTEST = """\
======================================================================
FAIL: test_sub (tests.test_calc.CalcTests)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_calc.py", line 9, in test_sub
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 4 tests in 0.002s

FAILED (failures=1, skipped=1)
"""


def _counts(framework: str, text: str) -> tuple[int | None, int | None, int | None]:
    summary = parse_test_output(framework, text)
    assert summary is not None
    return summary.passed, summary.failed, summary.skipped


def test_pytest_summary_counts_failures_and_errors_together() -> None:
    assert _counts("pytest", PYTEST_FAILING) == (8, 2, 1)


def test_pytest_failures_are_cases_with_their_message() -> None:
    summary = parse_test_output("pytest", PYTEST_FAILING)
    assert summary is not None
    assert summary.cases == (
        TestCaseResult("tests/test_a.py::test_login_fails", "failed", "assert 401 == 200"),
        TestCaseResult(
            "tests/test_b.py::test_fixture_broken", "error", "RuntimeError: no database"
        ),
    )


def test_pytest_quiet_pass_has_counts_and_no_cases() -> None:
    summary = parse_test_output("pytest", PYTEST_QUIET_OK)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (8, 0, 0)
    assert summary.cases == ()


def test_pytest_verbose_lines_name_passed_and_skipped_tests_too() -> None:
    summary = parse_test_output("pytest", PYTEST_VERBOSE)  # includes colour codes
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (1, 1, 1)
    outcomes = {c.name: (c.outcome, c.message) for c in summary.cases}
    assert outcomes["tests/test_a.py::test_two"] == ("failed", "AssertionError: nope")
    assert outcomes["tests/test_a.py::test_one"] == ("passed", None)
    assert outcomes["tests/test_a.py::test_three"][0] == "skipped"


def test_pytest_no_tests_ran_is_zero_not_unknown() -> None:
    assert _counts("pytest", "============ no tests ran in 0.01s ============\n") == (0, 0, 0)


def test_jest_counts_and_marked_cases() -> None:
    summary = parse_test_output("jest", JEST)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (1, 1, 1)
    by_name = {c.name: c for c in summary.cases}
    assert by_name["subtracts"].outcome == "failed"
    assert by_name["subtracts"].message == "expect(received).toBe(expected) // Object.is equality"
    assert by_name["adds"].outcome == "passed"
    assert by_name["skipped multiplies"].outcome == "skipped"
    assert summary.cases[0].name == "subtracts"  # failures come first


def test_jest_without_verbose_names_the_failure_from_its_block() -> None:
    summary = parse_test_output("jest", JEST_QUIET_FAILURE)
    assert summary is not None
    assert (summary.passed, summary.failed) == (4, 1)
    assert [c.name for c in summary.cases] == ["calculator › divides by zero"]


def test_vitest_totals_use_pipes() -> None:
    summary = parse_test_output("vitest", VITEST)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (5, 1, 2)
    failed = [c for c in summary.cases if c.outcome == "failed"]
    assert failed and failed[0].message == "AssertionError: expected 1 to be 2"


def test_npm_test_tries_every_parser() -> None:
    assert _counts("npm test", JEST) == (1, 1, 1)
    assert _counts("npm test", PYTEST_QUIET_OK) == (8, 0, 0)


def test_go_verbose_counts_every_result_line() -> None:
    summary = parse_test_output("go test", GO_VERBOSE)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (1, 1, 1)
    divide = next(c for c in summary.cases if c.name == "TestDivide")
    assert divide.message == "calc_test.go:21: want 2, got 0"


def test_go_without_verbose_does_not_guess_the_passed_count() -> None:
    summary = parse_test_output("go test", GO_QUIET_FAILURE)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (None, 1, None)


def test_go_pass_without_verbose_has_nothing_to_read() -> None:
    assert parse_test_output("go test", GO_QUIET_OK) is None


def test_cargo_adds_up_every_test_result_line() -> None:
    summary = parse_test_output("cargo test", CARGO)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (2, 1, 1)
    divides = next(c for c in summary.cases if c.name == "tests::divides")
    assert divides.message == "assertion `left == right` failed"


def test_dotnet_new_summary_line_and_failed_case() -> None:
    summary = parse_test_output("dotnet test", DOTNET)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (4, 1, 1)
    assert summary.cases == (
        TestCaseResult(
            "Calc.Tests.DivideTests.ByZero",
            "failed",
            "System.DivideByZeroException : Attempted to divide by zero.",
        ),
    )


def test_dotnet_old_summary_block() -> None:
    assert _counts("dotnet test", DOTNET_OLD) == (3, 2, 0)


def test_unittest_counts_from_ran_and_failed_lines() -> None:
    summary = parse_test_output("unittest", UNITTEST)
    assert summary is not None
    assert (summary.passed, summary.failed, summary.skipped) == (2, 1, 1)
    assert summary.cases == (
        TestCaseResult("test_sub (tests.test_calc.CalcTests)", "failed", "AssertionError: 1 != 2"),
    )


def test_unknown_runner_and_unreadable_output_give_nothing() -> None:
    assert parse_test_output("maven", PYTEST_FAILING) is None
    assert parse_test_output("pytest", "Traceback (most recent call last):\nImportError\n") is None


def test_cases_are_capped_failures_first_and_messages_are_cut() -> None:
    lines = [f"tests/t.py::test_{i} PASSED" for i in range(600)]
    lines.append("FAILED tests/t.py::test_bad - " + "x" * 1000)
    lines.append("=== 1 failed, 600 passed in 5.0s ===")
    summary = parse_test_output("pytest", "\n".join(lines))
    assert summary is not None
    assert len(summary.cases) == MAX_CASES
    assert summary.cases[0].name == "tests/t.py::test_bad"
    assert summary.cases[0].message is not None
    assert len(summary.cases[0].message) == MAX_MESSAGE
    assert summary.passed == 600  # the count is exact even when the cases are capped
