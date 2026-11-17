"""File roles and test counting."""

import pytest

from ccp.index.fileroles import count_tests_added, file_role


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("tests/unit/test_a.py", "test"),
        ("src/app/test_models.py", "test"),
        ("src/app/models_test.py", "test"),
        ("pkg/calc_test.go", "test"),
        ("web/src/button.spec.tsx", "test"),
        ("web/src/button.test.js", "test"),
        ("web/__tests__/button.js", "test"),
        ("src/test/java/AppTest.java", "test"),
        ("tests/README.md", "test"),  # test comes first
        ("README.md", "docs"),
        ("docs/design/overview.png", "docs"),
        ("docs/conf.py", "docs"),
        ("pyproject.toml", "config"),
        ("deploy/app.yaml", "config"),
        ("ci/build.yml", "config"),
        ("package.json", "config"),
        ("setup.cfg", "config"),
        ("tox.ini", "config"),
        (".env", "config"),
        (".env.local", "config"),
        ("infra/Dockerfile", "config"),
        ("src/ccp/index/worker.py", "code"),
        ("cmd/main.go", "code"),
        ("src/lib.rs", "code"),
        ("web/app.tsx", "code"),
        ("Contest.java", "code"),  # "test" inside a word is not a test file
        ("data/blob.bin", "other"),
        ("LICENSE", "other"),
        ("C:\\work\\app\\tests\\test_a.py", "test"),
        ("", "other"),
    ],
)
def test_a_path_gets_its_role(path: str, role: str) -> None:
    assert file_role(path) == role


def test_lines_that_define_tests_are_counted_in_every_language() -> None:
    lines = [
        "def test_login():",
        "    async def test_nested(self):",
        "async def test_fetch():",
        "it('works', () => {",
        "  test.only('one', () => {",
        "describe('suite', () => {",
        "    @Test",
        "func TestAdd(t *testing.T) {",
        "#[test]",
        "#[tokio::test]",
    ]
    assert count_tests_added(lines) == len(lines)


def test_calls_helpers_and_text_are_not_tests() -> None:
    lines = [
        "def helper():",
        "result = test(1)",
        "    x = it(2)",
        "# def test_commented():",
        'print("def test_in_string():")',
        "def testing_utils():",
        "",
    ]
    assert count_tests_added(lines) == 0


def test_a_name_that_only_starts_like_a_test_func_counts_as_go_does() -> None:
    # Go runs every function named Test<Something>; Testify is one.
    assert count_tests_added(["func Testify(t *testing.T) {}"]) == 1
