"""Upload the chats that already exist on this machine (``ccp-capture backfill``).

Claude Code keeps one transcript per chat at
``~/.claude/projects/<project folder>/<chat id>.jsonl``;
sub-agent transcripts and large tool results live in the folder ``<chat id>/`` next to it. The
hooks only upload chats from the day they are installed. Backfill sends the older ones, using the
same :meth:`ccp.capture.client.CaptureClient.sync_session` as the hooks: everything is redacted
before it leaves the machine, and the saved upload positions make it safe to run again (what was
uploaded is skipped).

``--dry-run`` uploads nothing: it only counts what would be sent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ccp.capture.client import CaptureClient, StreamResult
from ccp.capture.state import StateStore
from ccp.storage.keys import MAIN_STREAM, TOOL_RESULTS_FOLDER, check_id

# One chat may be large: give it far more time than a hook gets. A chat that still is not done
# is reported as incomplete, and running backfill again carries on where it stopped.
SESSION_BUDGET_SECONDS = 3600.0


def default_projects_dir() -> Path:
    """Return Claude Code's transcript folder, ``~/.claude/projects``."""
    return Path.home() / ".claude" / "projects"


@dataclass(frozen=True)
class Transcript:
    """One chat found on disk."""

    project: str  # the project folder's name (Claude Code's encoding of the working folder)
    session_id: str
    path: Path


@dataclass
class ProjectReport:
    """What happened to the chats of one project folder."""

    found: int = 0  # chats looked at
    uploaded: int = 0  # chats where something new was sent (dry run: would be sent)
    unchanged: int = 0  # chats already fully uploaded
    incomplete: int = 0  # chats not fully uploaded (out of time, or an error)
    chunks: int = 0
    tool_results: int = 0


@dataclass
class BackfillReport:
    """The outcome of a backfill run.

    Attributes:
        projects: One report per project folder, by name.
        errors: Short reasons (no transcript content) why something was not uploaded.
        stopped: Why the run stopped before the end (API unreachable or token refused), or None.
    """

    projects: dict[str, ProjectReport] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stopped: str | None = None

    @property
    def ok(self) -> bool:
        """True if nothing went wrong."""
        return not self.errors and self.stopped is None


def find_transcripts(
    projects_dir: Path, since_days: float | None = None, now: float | None = None
) -> list[Transcript]:
    """Find every chat transcript ``<projects_dir>/*/*.jsonl``, oldest project folder name first.

    Args:
        projects_dir: Claude Code's ``projects`` folder.
        since_days: Only chats changed within this many days (by file time); ``None`` for all.
        now: The current time in seconds (replaced in tests).

    Returns:
        The chats whose file name is a usable session id, in a stable order. Files with another
        name are not chats and are left out.
    """
    if not projects_dir.is_dir():
        return []
    cutoff = (
        None if since_days is None else (time.time() if now is None else now) - since_days * 86400
    )
    found = []
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        try:
            session_id = check_id("session id", path.stem)
            stat = path.stat()
        except (ValueError, OSError):
            continue
        if cutoff is not None and stat.st_mtime < cutoff:
            continue
        found.append(Transcript(path.parent.name, session_id, path))
    return found


def pending_bytes(state: StateStore, transcript: Transcript) -> int:
    """Count the source bytes of a chat (main and sub-agent transcripts) not uploaded yet."""
    total = _remaining(state, transcript.session_id, MAIN_STREAM, transcript.path)
    folder = transcript.path.with_suffix("") / "subagents"
    if folder.is_dir():
        for path in sorted(folder.rglob("agent-*.jsonl")):
            stream = path.name.removesuffix(".jsonl")
            try:
                check_id("stream", stream)
            except ValueError:
                continue
            total += _remaining(state, transcript.session_id, stream, path)
    return total


def _remaining(state: StateStore, session_id: str, stream: str, path: Path) -> int:
    """Bytes of one file past its saved upload position."""
    try:
        return max(0, path.stat().st_size - state.get_offset(session_id, stream))
    except OSError:
        return 0


def backfill(
    client: CaptureClient,
    state: StateStore,
    projects_dir: Path,
    *,
    since_days: float | None = None,
    dry_run: bool = False,
    on_project: Callable[[str, ProjectReport], None] | None = None,
) -> BackfillReport:
    """Upload every chat found under ``projects_dir`` that is not fully uploaded yet.

    The run stops at the first sign that the API is down or refuses the token (every other chat
    would fail the same way); what was done up to then is in the report.

    Args:
        client: The capture client (its API is real, or a fake in tests).
        state: The client's state, where the upload positions are.
        projects_dir: Claude Code's ``projects`` folder.
        since_days: Only chats changed within this many days.
        dry_run: Count what would be uploaded, and upload nothing.
        on_project: Called with each project's report once its chats are done (for printing).
    """
    report = BackfillReport()
    current: str | None = None
    for transcript in find_transcripts(projects_dir, since_days):
        if transcript.project != current:
            if current is not None and on_project is not None:
                on_project(current, report.projects[current])
            current = transcript.project
        project = report.projects.setdefault(transcript.project, ProjectReport())
        project.found += 1
        if dry_run:
            if pending_bytes(state, transcript):
                project.uploaded += 1
            else:
                project.unchanged += 1
            continue
        results = client.sync_session(
            transcript.session_id, transcript.path, SESSION_BUDGET_SECONDS
        )
        _count(project, results)
        stop = next((r for r in results if r.stops_run), None)
        if stop is not None:
            report.stopped = stop.error or "the API is unavailable"
            break
        report.errors += [
            f"{transcript.session_id[:8]} {r.stream}: {r.error}" for r in results if r.error
        ]
    if current is not None and on_project is not None:
        on_project(current, report.projects[current])
    return report


def _count(project: ProjectReport, results: list[StreamResult]) -> None:
    """Add the results of one chat to its project's counts."""
    chunks = sum(r.chunks for r in results)
    files = sum(r.files for r in results)
    project.chunks += chunks
    project.tool_results += files
    # Tool results are paused (not an error) when an older API lacks their endpoint.
    if any(r.error or (not r.caught_up and r.stream != TOOL_RESULTS_FOLDER) for r in results):
        project.incomplete += 1
    elif chunks or files or sum(r.attachments for r in results):
        project.uploaded += 1
    else:
        project.unchanged += 1
