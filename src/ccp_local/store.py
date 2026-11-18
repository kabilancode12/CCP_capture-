"""Where captures live on disk, and small file helpers.

Layout (one folder per project, nothing leaves the machine):

    ~/.ccp/captures/
      <project>__<hash8>/
        project.json            path of the project and when it was first seen
        activity.jsonl          one line per hook event, all sessions of the project
        shadow.git/             separate local Git repo: a snapshot after every change
        sessions/<date>__<id8>/
          events.jsonl          every hook payload, verbatim, plus the snapshot it made
          raw/                  byte-exact copy of Claude Code's transcript + sub-agent files
          capture.json          structured turn-by-turn record (built from raw/ + events)
          report.md, report.html
      .sessions/<session_id>    which project folder a session belongs to
      errors.log                hook errors (a hook never breaks Claude Code)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def home() -> Path:
    """Root folder of all captures (override with CCP_LOCAL_HOME)."""
    return Path(os.environ.get("CCP_LOCAL_HOME") or Path.home() / ".ccp" / "captures")


def now() -> str:
    """UTC time in the same format Claude Code writes in transcripts."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def project_root(cwd: str) -> Path:
    """Git top-level of `cwd`, else `cwd` itself."""
    path = Path(cwd).resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            path = Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return path


def project_dir(root: Path) -> Path:
    """Folder for the project whose root is `root` (readable name + short hash of the path)."""
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:8]
    return home() / f"{root.name or 'root'}__{digest}"


def session_project(session_id: str, cwd: str) -> tuple[Path, dict]:
    """Project folder + project.json of a session, pinned at its first event.

    A later `cd` does not split the session. Snapshots are off for the home folder or a drive root.
    """
    pin = home() / ".sessions" / session_id
    if pin.exists():
        pdir = Path(pin.read_text(encoding="utf-8").strip())
        return pdir, json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    root = project_root(cwd)
    pdir = project_dir(root)
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(str(pdir), encoding="utf-8")
    meta = pdir / "project.json"
    if not meta.exists():
        big = root == Path.home().resolve() or root == Path(root.anchor)
        write_json(
            meta, {"root": str(root), "name": root.name, "first_seen": now(), "snapshots": not big}
        )
    return pdir, json.loads(meta.read_text(encoding="utf-8"))


def session_dir(pdir: Path, session_id: str) -> Path:
    """Folder of one session: `<first date>__<first 8 chars of the id>`."""
    sessions = pdir / "sessions"
    found = sorted(sessions.glob(f"*__{session_id[:8]}")) if sessions.exists() else []
    if found:
        return found[0]
    sdir = sessions / f"{now()[:10]}__{session_id[:8]}"
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON line (one short write, safe enough for concurrent hooks)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """All JSON lines of a file; broken lines are skipped."""
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def write_json(path: Path, data: object) -> None:
    """Write pretty-printed JSON, creating folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def copy_raw(transcript: Path, sdir: Path) -> None:
    """Copy the transcript and its sibling folder (sub-agents, large tool results) byte-exact."""
    raw = sdir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    if transcript.is_file():
        shutil.copy2(transcript, raw / "transcript.jsonl")
    extra = transcript.with_suffix("")
    if extra.is_dir():
        shutil.copytree(extra, raw / extra.name, dirs_exist_ok=True)


def log_error(message: str) -> None:
    """Note a hook error; never raises."""
    with contextlib.suppress(OSError):
        append_jsonl(home() / "errors.log", {"time": now(), "error": message})
