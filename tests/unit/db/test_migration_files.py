from pathlib import Path

import pytest

from ccp.db.migrations import MigrationError, discover_migrations

REAL_MIGRATIONS = Path(__file__).parents[3] / "db" / "migrations"


def _write(folder: Path, **files: str) -> None:
    """Create files named by keyword (double underscore = dot): 001_x__down__sql is not used;
    pass names via ``folder / name`` instead when odd characters are needed."""
    for name, text in files.items():
        (folder / name.replace("__", ".")).write_text(text, encoding="utf-8")


def test_migrations_are_found_in_version_order(tmp_path: Path) -> None:
    _write(tmp_path, **{"002_add_b__sql": "x", "001_add_a__sql": "x", "001_add_a__down__sql": "x"})
    found = discover_migrations(tmp_path)
    assert [m.label for m in found] == ["001_add_a", "002_add_b"]
    assert found[0].down_path is not None  # has a down file
    assert found[1].down_path is None  # has none


def test_missing_folder_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="not found"):
        discover_migrations(tmp_path / "nope")


@pytest.mark.parametrize("name", ["1_short.sql", "001_Upper.sql", "001-dash.sql", "notes.sql"])
def test_badly_named_file_is_an_error(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("x", encoding="utf-8")
    with pytest.raises(MigrationError, match="Unexpected file name"):
        discover_migrations(tmp_path)


def test_a_gap_in_versions_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, **{"001_a__sql": "x", "003_c__sql": "x"})
    with pytest.raises(MigrationError, match="without gaps"):
        discover_migrations(tmp_path)


def test_versions_must_start_at_one(tmp_path: Path) -> None:
    _write(tmp_path, **{"002_b__sql": "x"})
    with pytest.raises(MigrationError, match="without gaps"):
        discover_migrations(tmp_path)


def test_duplicate_version_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, **{"001_a__sql": "x", "001_b__sql": "x"})
    with pytest.raises(MigrationError, match="Duplicate"):
        discover_migrations(tmp_path)


def test_down_file_without_up_file_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, **{"001_a__sql": "x", "002_b__down__sql": "x"})
    with pytest.raises(MigrationError, match="no matching up file"):
        discover_migrations(tmp_path)


def test_down_file_with_a_different_name_is_an_error(tmp_path: Path) -> None:
    _write(tmp_path, **{"001_a__sql": "x", "001_other__down__sql": "x"})
    with pytest.raises(MigrationError, match="no matching up file"):
        discover_migrations(tmp_path)


def test_checksum_changes_with_content_but_not_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "001_a.sql"
    path.write_bytes(b"CREATE TABLE t (a int);\n")
    original = discover_migrations(tmp_path)[0].checksum

    path.write_bytes(b"CREATE TABLE t (a int);\r\n")  # Windows line endings
    assert discover_migrations(tmp_path)[0].checksum == original

    path.write_bytes(b"CREATE TABLE t (b int);\n")  # different content
    assert discover_migrations(tmp_path)[0].checksum != original


def test_the_real_migrations_folder_is_valid() -> None:
    found = discover_migrations(REAL_MIGRATIONS)
    assert found[0].label == "001_initial_schema"
    assert all(m.down_path is not None for m in found)  # every migration can be undone
