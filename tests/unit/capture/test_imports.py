"""The capture client runs on developers' machines and must stay light.

It is started afresh for every Claude Code hook, so importing server-only packages (MinIO and its
compiled dependencies, the PostgreSQL driver, the web framework) would slow every hook and force
those packages onto every developer machine.
"""

import subprocess
import sys

SERVER_ONLY = ["minio", "pg8000", "starlette", "uvicorn", "urllib3", "argon2", "Crypto", "httpx"]


def _modules_loaded_by(import_statement: str) -> set[str]:
    """Run ``import_statement`` in a fresh interpreter and return the top-level modules loaded."""
    code = (
        f"{import_statement}; import sys; "
        "print(' '.join(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    result = subprocess.run(  # noqa: S603  (fixed arguments)
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    return set(result.stdout.split())


def test_the_capture_command_imports_no_server_only_package() -> None:
    loaded = _modules_loaded_by("import ccp.capture.cli")
    assert not loaded & set(SERVER_ONLY), loaded & set(SERVER_ONLY)


def test_the_storage_package_does_not_load_the_minio_client_until_needed() -> None:
    loaded = _modules_loaded_by(
        "import ccp.storage; import ccp.storage.keys; import ccp.storage.attachments"
    )
    assert "minio" not in loaded


def test_the_object_store_names_still_work_through_the_package() -> None:
    from ccp.storage import ObjectNotFoundError, ObjectStore, StorageError, connect_store

    assert issubclass(ObjectNotFoundError, StorageError)
    assert ObjectStore.__name__ == "ObjectStore" and callable(connect_store)  # loaded on use


def test_an_unknown_name_in_the_storage_package_is_an_attribute_error() -> None:
    import ccp.storage

    assert not hasattr(ccp.storage, "does_not_exist")
