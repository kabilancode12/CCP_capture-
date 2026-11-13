"""Guards against documentation drift.

A document that names a command that no longer exists, a setting that was renamed, or a page that was
moved sends a new developer in the wrong direction. These tests read the documents and check them
against the code:

* every relative link points at a file (and, if it names a heading, at a heading) that exists;
* every ``ccp-...`` command the documents mention is a real command with a real sub-command;
* every setting (environment variable) the documents mention exists in the code or the examples.

The first project's copies in ``docs/reference/`` are history, not instructions, and are not checked.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "docs" / "verify").glob("*.md")),
]

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
COMMAND = re.compile(r"`(ccp-(?:db|ingest|index|audit|capture|verify))(?:\s+([a-z][a-z-]*))?")
SETTING = re.compile(r"`((?:CCP|POSTGRES|MINIO)_[A-Z_]+)`")


def _anchors(markdown: Path) -> set[str]:
    """The heading anchors of a Markdown file, the way GitHub makes them."""
    anchors = set()
    in_code = False
    for line in markdown.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        match = re.match(r"#{1,6}\s+(.*)", line)
        if match and not in_code:
            text = re.sub(r"[`*_]", "", match.group(1)).strip().lower()
            text = re.sub(r"[^\w\s-]", "", text)
            anchors.add(re.sub(r"\s", "-", text))
    return anchors


def _id(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


@pytest.mark.parametrize("document", DOCUMENTS, ids=_id)
def test_relative_links_point_at_existing_files_and_headings(document: Path) -> None:
    problems = []
    for target in LINK.findall(document.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, fragment = target.partition("#")
        linked = document if not path_part else (document.parent / path_part).resolve()
        if not linked.exists():
            problems.append(f"{target}: no such file")
        elif fragment and linked.suffix == ".md" and fragment not in _anchors(linked):
            problems.append(f"{target}: no such heading")
    assert not problems, "\n".join(problems)


def _scripts() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(project["project"]["scripts"])


def _subcommands() -> dict[str, set[str]]:
    from ccp.audit.cli import build_parser as audit_parser
    from ccp.capture.cli import build_parser as capture_parser
    from ccp.db.cli import build_parser as db_parser
    from ccp.index.cli import build_parser as index_parser
    from ccp.ingest.server import build_parser as ingest_parser
    from ccp.verify.cli import build_parser as verify_parser

    def names(parser: object) -> set[str]:
        action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")  # type: ignore[attr-defined]
        return set(action.choices)

    return {
        "ccp-audit": names(audit_parser()),
        "ccp-capture": names(capture_parser()),
        "ccp-db": names(db_parser()),
        "ccp-index": names(index_parser()),
        "ccp-ingest": names(ingest_parser()),
        "ccp-verify": names(verify_parser()),
    }


def test_every_command_the_documents_mention_exists() -> None:
    scripts, subcommands = _scripts(), _subcommands()
    problems = []
    for document in DOCUMENTS:
        for command, sub in COMMAND.findall(document.read_text(encoding="utf-8")):
            if command not in scripts:
                problems.append(f"{_id(document)}: {command} is not a command")
            elif sub and sub not in subcommands[command]:
                problems.append(f"{_id(document)}: `{command} {sub}` is not a sub-command")
    assert not problems, "\n".join(problems)


def test_every_setting_the_documents_mention_exists() -> None:
    known = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            *sorted((ROOT / "src").rglob("*.py")),
            ROOT / ".env.example",
            ROOT / "infra" / "docker-compose.yml",
        ]
    )
    problems = []
    for document in DOCUMENTS:
        for name in set(SETTING.findall(document.read_text(encoding="utf-8"))):
            if name not in known:
                problems.append(f"{_id(document)}: {name} is not a setting")
    assert not problems, "\n".join(problems)


def test_every_setting_the_code_reads_is_documented() -> None:
    settings_code = (ROOT / "src" / "ccp" / "settings.py").read_text(encoding="utf-8")
    documented = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    names = set(re.findall(r'"((?:CCP|POSTGRES|MINIO)_[A-Z_]+)"', settings_code))
    missing = sorted(name for name in names if f"`{name}`" not in documented)
    assert not missing, f"not described in docs/configuration.md: {missing}"


def test_the_environment_example_sets_every_required_server_setting() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "MINIO_ACCESS_KEY",
                 "MINIO_SECRET_KEY", "CCP_INGEST_TOKEN"):  # fmt: skip
        assert re.search(rf"^{name}=", example, re.MULTILINE), (
            f"{name} is missing from .env.example"
        )
