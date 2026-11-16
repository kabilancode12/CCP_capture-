"""Start the watcher at login, on every operating system.

``ccp-capture watch-service install`` registers ``ccp-capture watch`` with the system's own
mechanism:

* Linux: a systemd **user** unit (``~/.config/systemd/user/ccp-capture-watch.service``), enabled;
* macOS: a launchd agent (``~/Library/LaunchAgents/com.ccp.capture-watch.plist``), loaded;
* Windows: a Task Scheduler task that runs at logon (``schtasks``).

None of them needs administrator rights. The file texts are made by pure functions
(:func:`systemd_unit`, :func:`launchd_plist`, :func:`schtasks_arguments`) so that they are tested
without touching the system.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

NAME = "ccp-capture-watch"
LAUNCHD_LABEL = "com.ccp.capture-watch"


def command() -> list[str]:
    """The command that runs the watcher with the Python that runs this one."""
    return [Path(sys.executable).as_posix(), "-m", "ccp.capture", "watch"]


def systemd_unit(args: list[str]) -> str:
    """A systemd user unit that keeps the watcher running."""
    exec_start = " ".join(f'"{a}"' if " " in a else a for a in args)
    return (
        "[Unit]\nDescription=CCP capture watcher (records changes no hook sees)\n\n"
        f"[Service]\nType=simple\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def launchd_plist(args: list[str]) -> str:
    """A launchd agent that starts the watcher at login and keeps it alive."""
    items = "".join(f"    <string>{a}</string>\n" for a in args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{items}  </array>\n"
        "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n</dict>\n</plist>\n"
    )


def schtasks_arguments(args: list[str]) -> list[str]:
    """The ``schtasks`` arguments that create the logon task."""
    run = " ".join(f'\\"{a}\\"' if " " in a else a for a in args)
    return ["/Create", "/F", "/SC", "ONLOGON", "/TN", NAME, "/TR", run]


def _run(arguments: list[str]) -> bool:
    executable = shutil.which(arguments[0])
    if executable is None:
        return False
    done = subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [executable, *arguments[1:]], capture_output=True, check=False
    )
    return done.returncode == 0


def install(home: Path | None = None, platform: str | None = None) -> list[str]:
    """Register the watcher for this operating system; return notes for the user."""
    home, platform = home or Path.home(), platform or sys.platform
    args = command()
    if platform.startswith("linux"):
        path = home / ".config" / "systemd" / "user" / f"{NAME}.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(systemd_unit(args), encoding="utf-8")
        ok = _run(["systemctl", "--user", "daemon-reload"]) and _run(
            ["systemctl", "--user", "enable", "--now", f"{NAME}.service"]
        )
        return [f"wrote {path}"] + (
            ["enabled and started (systemctl --user status " + NAME + ")"]
            if ok
            else [f"run: systemctl --user enable --now {NAME}.service"]
        )
    if platform == "darwin":
        path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launchd_plist(args), encoding="utf-8")
        loaded = _run(["launchctl", "load", "-w", str(path)])
        return [f"wrote {path}"] + ([] if loaded else [f"run: launchctl load -w {path}"])
    if platform.startswith("win"):
        ok = _run(["schtasks", *schtasks_arguments(args)])
        return [
            "scheduled task created (runs at logon)" if ok else "schtasks failed: run it by hand"
        ]
    return [f"unsupported system {platform}: run 'python -m ccp.capture watch' yourself"]


def uninstall(home: Path | None = None, platform: str | None = None) -> list[str]:
    """Remove what :func:`install` registered."""
    home, platform = home or Path.home(), platform or sys.platform
    if platform.startswith("linux"):
        _run(["systemctl", "--user", "disable", "--now", f"{NAME}.service"])
        (home / ".config" / "systemd" / "user" / f"{NAME}.service").unlink(missing_ok=True)
        return ["watcher service removed"]
    if platform == "darwin":
        path = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        _run(["launchctl", "unload", "-w", str(path)])
        path.unlink(missing_ok=True)
        return ["watcher agent removed"]
    if platform.startswith("win"):
        _run(["schtasks", "/Delete", "/F", "/TN", NAME])
        return ["scheduled task removed"]
    return ["nothing to remove"]
