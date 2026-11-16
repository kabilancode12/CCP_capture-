"""The start-at-login files for the watcher (made without touching the system)."""

from pathlib import Path

from ccp.capture import watch_service


def test_the_systemd_unit_runs_the_watcher_and_restarts_it() -> None:
    unit = watch_service.systemd_unit(["/opt/my python/bin/python", "-m", "ccp.capture", "watch"])
    assert 'ExecStart="/opt/my python/bin/python" -m ccp.capture watch' in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit


def test_the_launchd_agent_starts_at_login_and_stays_alive() -> None:
    plist = watch_service.launchd_plist(["/usr/bin/python3", "-m", "ccp.capture", "watch"])
    assert "<string>-m</string>" in plist and "<key>RunAtLoad</key><true/>" in plist
    assert watch_service.LAUNCHD_LABEL in plist and "<key>KeepAlive</key><true/>" in plist


def test_the_windows_task_runs_at_logon() -> None:
    arguments = watch_service.schtasks_arguments(
        ["C:/Program Files/Python/python.exe", "-m", "ccp.capture", "watch"]
    )
    assert arguments[:6] == ["/Create", "/F", "/SC", "ONLOGON", "/TN", "ccp-capture-watch"]
    assert '\\"C:/Program Files/Python/python.exe\\" -m ccp.capture watch' in arguments[-1]


def test_installing_on_linux_writes_the_unit_and_uninstall_removes_it(
    tmp_path: Path, monkeypatch: object
) -> None:
    monkeypatch.setattr(watch_service, "_run", lambda arguments: False)  # type: ignore[attr-defined]
    notes = watch_service.install(tmp_path, "linux")
    unit = tmp_path / ".config" / "systemd" / "user" / "ccp-capture-watch.service"
    assert unit.is_file() and "systemctl --user enable --now" in "\n".join(notes)
    watch_service.uninstall(tmp_path, "linux")
    assert not unit.exists()
    assert "unsupported" in watch_service.install(tmp_path, "plan9")[0]
