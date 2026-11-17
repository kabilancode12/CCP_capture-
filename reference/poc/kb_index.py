#!/usr/bin/env python3
"""
kb_index.py - build the structured index (layer 2) from captured Claude sessions.

Reads:
  * transcripts in the outbox      (one folder per session, transcript.jsonl inside)
  * capture projects (Git)         (snapshot refs and branch backups)
Writes:
  * a SQLite database with sessions, messages, tool calls, snapshots and file changes,
    plus empty knowledge-base tables (layer 3) that curation will fill later.

Layer 2 is derived data: every `build` rebuilds it from the raw capture.
Layer 3 tables are never dropped; they refer to layer 2 by natural keys
(session ID, message UUID, tool-use ID, snapshot commit), so links survive rebuilds.

Usage:
  python kb_index.py build   [--db PATH] [--outbox DIR] [--capture PATH ...] [--state DIR]
  python kb_index.py report  [--db PATH]
  python kb_index.py export  SESSION_ID [--db PATH] [--out FILE]
  python kb_index.py query   "SQL" [--db PATH]

--capture accepts a capture project (bare or normal Git repository) or a folder that
contains capture projects (every *.git inside is loaded). Repeat it for several.
Requires Python 3.8+ and git on PATH. No other dependencies.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".claude-capture"))
DEFAULT_DB = HOME / "capture-index.db"
DEFAULT_OUTBOX = Path(os.environ.get("CAPTURE_OUTBOX", HOME / "outbox"))
DEFAULT_STATE = HOME / "state"

MARKER_RE = re.compile(
    r"capture_id=(?P<capture_id>\S+)\s+repository=(?P<repository>\S+)\s+branch=(?P<branch>\S+)"
    r"\s+commit=(?P<commit>\S+)\s+developer=(?P<developer>[^\s\"\\]+)")

# ----------------------------------------------------------------------------- schema

LAYER2 = """
CREATE TABLE repository (
  name              TEXT PRIMARY KEY,
  capture_location  TEXT
);
CREATE TABLE developer (
  email             TEXT PRIMARY KEY
);
CREATE TABLE session (
  session_id        TEXT PRIMARY KEY,
  capture_id        TEXT,
  developer_email   TEXT,
  repository        TEXT,
  branch            TEXT,
  start_commit      TEXT,
  started_at        TEXT,
  ended_at          TEXT,
  title             TEXT,
  source            TEXT,          -- 'hook' now; 'compliance_api' in Phase 1b
  transcript_path   TEXT,
  message_count     INTEGER DEFAULT 0,
  tool_call_count   INTEGER DEFAULT 0,
  snapshot_count    INTEGER DEFAULT 0
);
CREATE TABLE message (
  uuid              TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  seq               INTEGER NOT NULL,
  parent_uuid       TEXT,
  role              TEXT,          -- user | assistant | system
  kind              TEXT,          -- prompt | response | tool_result | marker | other
  is_sidechain      INTEGER,       -- 1 = sub-agent activity
  timestamp         TEXT,
  text              TEXT
);
CREATE TABLE tool_call (
  tool_use_id       TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  message_uuid      TEXT,
  seq               INTEGER,
  name              TEXT,
  file_path         TEXT,          -- for Read/Write/Edit-type tools
  command           TEXT,          -- for Bash/PowerShell-type tools
  input_json        TEXT,
  result_text       TEXT,
  is_error          INTEGER,
  timestamp         TEXT
);
CREATE TABLE snapshot (
  commit_id         TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  seq               INTEGER,
  ref               TEXT,
  repository        TEXT,
  parent_commit     TEXT,
  captured_at       TEXT,
  event             TEXT,
  branch            TEXT,
  excluded          TEXT,
  is_baseline       INTEGER DEFAULT 0   -- 1 = starting state, uncommitted work present before the session
);
CREATE TABLE file_change (
  snapshot_commit   TEXT NOT NULL,
  compared_to       TEXT,          -- previous snapshot in the session, or the start commit
  path              TEXT NOT NULL,
  change_type       TEXT,          -- added | modified | deleted | other
  lines_added       INTEGER,
  lines_removed     INTEGER
);
CREATE TABLE branch_backup (
  repository        TEXT,
  developer         TEXT,
  branch            TEXT,
  commit_id         TEXT
);
CREATE INDEX ix_message_session   ON message(session_id, seq);
CREATE INDEX ix_tool_session      ON tool_call(session_id, seq);
CREATE INDEX ix_snapshot_session  ON snapshot(session_id, seq);
CREATE INDEX ix_change_path       ON file_change(path);

CREATE VIEW v_session_overview AS
SELECT s.session_id, s.title, s.developer_email, s.repository, s.branch,
       substr(s.start_commit, 1, 10) AS start_commit, s.started_at, s.ended_at,
       s.message_count, s.tool_call_count, s.snapshot_count,
       (SELECT group_concat(DISTINCT fc.path)
          FROM file_change fc JOIN snapshot sn ON sn.commit_id = fc.snapshot_commit
         WHERE sn.session_id = s.session_id AND sn.is_baseline = 0) AS files_touched
FROM session s;

CREATE VIEW v_timeline AS
SELECT session_id, timestamp AS at, 'message' AS item, role || ':' || kind AS detail,
       substr(replace(text, char(10), ' '), 1, 120) AS summary
  FROM message WHERE kind IN ('prompt', 'response')
UNION ALL
SELECT session_id, timestamp, 'tool', name,
       coalesce(file_path, substr(command, 1, 120), '')
  FROM tool_call
UNION ALL
SELECT session_id, captured_at, CASE is_baseline WHEN 1 THEN 'baseline' ELSE 'snapshot' END, 'seq ' || seq,
       substr(commit_id, 1, 10) || ' ' ||
       coalesce((SELECT group_concat(change_type || ' ' || path, ', ')
                   FROM file_change WHERE snapshot_commit = commit_id), '')
  FROM snapshot;

CREATE VIEW v_file_history AS
SELECT fc.path, fc.change_type, fc.lines_added, fc.lines_removed,
       sn.captured_at, sn.session_id, sn.seq, substr(sn.commit_id, 1, 10) AS snapshot,
       s.developer_email,
       (SELECT text FROM message m
         WHERE m.session_id = sn.session_id AND m.kind = 'prompt'
           AND m.timestamp <= sn.captured_at
         ORDER BY m.timestamp DESC LIMIT 1) AS preceding_prompt
FROM file_change fc
JOIN snapshot sn ON sn.commit_id = fc.snapshot_commit AND sn.is_baseline = 0
LEFT JOIN session s ON s.session_id = sn.session_id;
"""

LAYER2_OBJECTS = ["v_file_history", "v_timeline", "v_session_overview",
                  "branch_backup", "file_change", "snapshot", "tool_call",
                  "message", "session", "developer", "repository"]

LAYER3 = """
CREATE TABLE IF NOT EXISTS knowledge_entry (
  entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_type    TEXT CHECK (entry_type IN ('answer', 'decision', 'artifact')),
  title         TEXT NOT NULL,
  problem       TEXT,
  context       TEXT,
  answer        TEXT,
  caveats       TEXT,
  not_applicable_when TEXT,
  status        TEXT DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'superseded', 'rejected')),
  review_by     TEXT,
  created_at    TEXT DEFAULT (datetime('now')),
  embedding     BLOB              -- semantic-search vector (pgvector in PostgreSQL)
);
CREATE TABLE IF NOT EXISTS evidence_link (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  evidence_type TEXT CHECK (evidence_type IN ('session', 'message', 'tool_call', 'snapshot')),
  evidence_key  TEXT              -- session_id, message uuid, tool_use_id or commit id
);
CREATE TABLE IF NOT EXISTS code_reference (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  repository    TEXT,
  start_commit  TEXT,
  final_snapshot TEXT,
  diff_text     TEXT
);
CREATE TABLE IF NOT EXISTS decision_link (
  from_entry    INTEGER REFERENCES knowledge_entry(entry_id),
  relation      TEXT CHECK (relation IN ('supersedes', 'depends_on', 'affects')),
  to_entry      INTEGER,
  to_text       TEXT              -- for assumptions or systems that are not entries
);
CREATE TABLE IF NOT EXISTS review (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  reviewer      TEXT,
  role          TEXT CHECK (role IN ('author', 'expert')),
  outcome       TEXT CHECK (outcome IN ('confirmed', 'approved', 'changes_requested', 'declined')),
  comment       TEXT,
  reviewed_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feedback (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  user_email    TEXT,
  helpful       INTEGER,
  comment       TEXT,
  given_at      TEXT DEFAULT (datetime('now'))
);
"""

# ----------------------------------------------------------------------------- helpers

def git(repo, *args):
    """Run git in a repository and return stdout ('' on failure)."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        return out.stdout if out.returncode == 0 else ""
    except FileNotFoundError:
        sys.exit("git was not found on PATH")


def content_text(content):
    """Flatten Claude message content (string or list of blocks) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(content_text(block.get("content")))
    return "\n".join(p for p in parts if p)


def find_marker(raw_line):
    m = MARKER_RE.search(raw_line)
    return m.groupdict() if m else None


def capture_projects(paths):
    """Expand --capture arguments into a list of Git repositories."""
    repos = []
    for p in paths:
        p = Path(p)
        if (p / "HEAD").exists() or (p / ".git").exists():
            repos.append(p)
        elif p.is_dir():
            repos.extend(sorted(q for q in p.iterdir() if q.is_dir() and (q / "HEAD").exists()))
    return repos


def repo_name(repo):
    name = Path(repo).name
    for suffix in (".git", "-capture"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name

# ----------------------------------------------------------------------------- loaders

def load_transcript(db, session_id, path, state_dir):
    """Parse one Claude Code transcript (JSON lines) into session/message/tool_call rows."""
    session = {"session_id": session_id, "source": "hook", "transcript_path": str(path)}
    seq = 0
    pending_tools = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            marker = find_marker(raw)
            if marker and not session.get("capture_id"):
                repo_from_marker = marker["repository"].rstrip("/").split("/")[-1].split(":")[-1]
                session.update(capture_id=marker["capture_id"], repository=repo_name(repo_from_marker),
                               branch=marker["branch"], start_commit=marker["commit"],
                               developer_email=marker["developer"])
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(line, dict):
                continue
            if line.get("type") == "summary" and line.get("summary"):
                session.setdefault("title", line["summary"])
                continue
            ts = line.get("timestamp")
            if ts:
                session["started_at"] = min(session.get("started_at") or ts, ts)
                session["ended_at"] = max(session.get("ended_at") or ts, ts)
            if line.get("gitBranch") and not session.get("branch"):
                session["branch"] = line["gitBranch"]
            msg = line.get("message")
            if not isinstance(msg, dict):
                if marker:
                    seq += 1
                    db.execute("INSERT OR REPLACE INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                               (line.get("uuid") or f"{session_id}:{seq}", session_id, seq,
                                line.get("parentUuid"), "system", "marker", 0, ts, raw[:2000]))
                continue
            seq += 1
            role = msg.get("role") or line.get("type")
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            is_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
            if marker:
                kind = "marker"
            elif role == "user" and is_tool_result:
                kind = "tool_result"
            elif role == "user" and not line.get("isMeta"):
                kind = "prompt"
            elif role == "assistant":
                kind = "response"
            else:
                kind = "other"
            uuid = line.get("uuid") or f"{session_id}:{seq}"
            db.execute("INSERT OR REPLACE INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                       (uuid, session_id, seq, line.get("parentUuid"), role, kind,
                        1 if line.get("isSidechain") else 0, ts, content_text(content)))
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    file_path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                    command = inp.get("command")
                    tid = b.get("id") or f"{uuid}:tool"
                    db.execute("INSERT OR REPLACE INTO tool_call VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (tid, session_id, uuid, seq, b.get("name"), file_path, command,
                                json.dumps(inp, ensure_ascii=False)[:20000], None, None, ts))
                    pending_tools[tid] = True
                elif b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        db.execute("UPDATE tool_call SET result_text = ?, is_error = ? WHERE tool_use_id = ?",
                                   (content_text(b.get("content"))[:20000],
                                    1 if b.get("is_error") else 0, tid))
    # Fall back to the hook's own state if the marker was not found in the transcript
    st = Path(state_dir) / session_id
    if not session.get("capture_id") and (st / "capture_id").exists():
        session["capture_id"] = (st / "capture_id").read_text().strip()
    if not session.get("start_commit") and (st / "start_commit").exists():
        session["start_commit"] = (st / "start_commit").read_text().strip()
    upsert_session(db, session)


def upsert_session(db, s):
    cols = ["session_id", "capture_id", "developer_email", "repository", "branch", "start_commit",
            "started_at", "ended_at", "title", "source", "transcript_path"]
    existing = db.execute("SELECT * FROM session WHERE session_id = ?", (s["session_id"],)).fetchone()
    if existing:
        sets = ", ".join(f"{c} = coalesce({c}, ?)" for c in cols[1:])
        db.execute(f"UPDATE session SET {sets} WHERE session_id = ?",
                   [s.get(c) for c in cols[1:]] + [s["session_id"]])
    else:
        db.execute(f"INSERT INTO session ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [s.get(c) for c in cols])


def load_capture_project(db, repo):
    """Load snapshot refs, their file changes and branch backups from one Git repository."""
    name = repo_name(repo)
    db.execute("INSERT OR IGNORE INTO repository VALUES (?, ?)", (name, str(repo)))
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/claude-capture")
    snaps = []
    for line in refs.splitlines():
        try:
            ref, commit = line.split(" ", 1)
        except ValueError:
            continue
        parts = ref.split("/")
        if len(parts) >= 5 and parts[2] == "sessions":
            snaps.append((parts[3], int(parts[4]) if parts[4].isdigit() else 0, ref, commit))
        elif len(parts) >= 6 and parts[2] == "users" and parts[4] == "heads":
            row = (name, parts[3], "/".join(parts[5:]), commit)
            if not db.execute("SELECT 1 FROM branch_backup WHERE repository=? AND developer=? AND branch=? AND commit_id=?",
                              row).fetchone():
                db.execute("INSERT INTO branch_backup VALUES (?,?,?,?)", row)
    snaps.sort()
    previous = {}
    for session_id, seq, ref, commit in snaps:
        info = git(repo, "log", "-1", "--format=%P%x00%cI%x00%B", commit)
        parent, committed_at, body = (info.split("\x00", 2) + ["", "", ""])[:3]
        parent = parent.split()[0] if parent.strip() else None
        fields = dict(re.findall(r"^([a-z-]+):\s*(.*)$", body, flags=re.M))
        captured_at = fields.get("captured-at") or committed_at.strip()
        event = fields.get("event") or ""
        baseline = 1 if event.replace("-", "").lower() == "sessionstart" else 0
        db.execute("INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (commit, session_id, seq, ref, name, parent, captured_at,
                    event, fields.get("branch"), fields.get("excluded"), baseline))
        db.execute("DELETE FROM file_change WHERE snapshot_commit = ?", (commit,))   # loaded from two places
        # Compare with the previous snapshot of this session, or with the start commit
        base = previous.get(session_id) or parent
        previous[session_id] = commit
        if base:
            status = git(repo, "diff", "--name-status", "--no-renames", base, commit)
            numstat = git(repo, "diff", "--numstat", "--no-renames", base, commit)
        else:
            empty = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # Git's empty tree
            status = git(repo, "diff", "--name-status", "--no-renames", empty, commit)
            numstat = git(repo, "diff", "--numstat", "--no-renames", empty, commit)
        counts = {}
        for nl in numstat.splitlines():
            bits = nl.split("\t")
            if len(bits) == 3:
                a, r, p = bits
                counts[p] = (int(a) if a.isdigit() else None, int(r) if r.isdigit() else None)
        kinds = {"A": "added", "M": "modified", "D": "deleted"}
        for sl in status.splitlines():
            bits = sl.split("\t")
            if len(bits) >= 2:
                code, p = bits[0], bits[-1]
                a, r = counts.get(p, (None, None))
                db.execute("INSERT INTO file_change VALUES (?,?,?,?,?,?)",
                           (commit, base, p, kinds.get(code[:1], "other"), a, r))
        # Make sure every snapshot has a session row, even without a transcript
        upsert_session(db, {"session_id": session_id, "repository": name,
                            "branch": fields.get("branch"),
                            "start_commit": fields.get("started-from") if fields.get("started-from") != "none" else None,
                            "capture_id": fields.get("capture-id") if fields.get("capture-id") != "unknown" else None,
                            "source": "hook"})


def build(args):
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    for obj in LAYER2_OBJECTS:                       # rebuild derived layer 2 only
        kind = "VIEW" if obj.startswith("v_") else "TABLE"
        db.execute(f"DROP {kind} IF EXISTS {obj}")
    db.executescript(LAYER2)
    db.executescript(LAYER3)                         # layer 3 is created once, never dropped

    outbox = Path(args.outbox)
    n_transcripts = 0
    if outbox.is_dir():
        for folder in sorted(p for p in outbox.iterdir() if p.is_dir()):
            t = folder / "transcript.jsonl"
            if t.exists():
                load_transcript(db, folder.name, t, args.state)
                n_transcripts += 1
    repos = capture_projects(args.capture)
    for repo in repos:
        load_capture_project(db, repo)

    db.execute("""INSERT OR IGNORE INTO developer
                  SELECT DISTINCT developer_email FROM session WHERE developer_email IS NOT NULL""")
    db.execute("""UPDATE session SET
        message_count  = (SELECT count(*) FROM message   WHERE message.session_id   = session.session_id AND kind IN ('prompt','response')),
        tool_call_count= (SELECT count(*) FROM tool_call WHERE tool_call.session_id = session.session_id),
        snapshot_count = (SELECT count(*) FROM snapshot  WHERE snapshot.session_id  = session.session_id AND is_baseline = 0)""")
    db.commit()
    totals = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("session", "message", "tool_call", "snapshot", "file_change", "branch_backup")}
    print(f"Index built: {db_path}")
    print(f"  transcripts read: {n_transcripts}   capture projects read: {len(repos)}")
    for t, n in totals.items():
        print(f"  {t:<14}{n}")
    db.close()

# ----------------------------------------------------------------------------- output

def print_rows(cur, max_width=60):
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cells = [[("" if v is None else str(v)).replace("\n", " ")[:max_width] for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    if not rows:
        print("(no rows)")


def report(args):
    db = sqlite3.connect(args.db)
    print("\nSESSIONS")
    print_rows(db.execute("""SELECT substr(session_id,1,8) AS session, developer_email, repository,
                             start_commit, message_count AS msgs, tool_call_count AS tools,
                             snapshot_count AS snaps, files_touched
                             FROM v_session_overview ORDER BY started_at"""))
    print("\nFILES CHANGED BY CLAUDE, WITH THE PROMPT THAT LED TO EACH CHANGE")
    print_rows(db.execute("""SELECT path, change_type, substr(session_id,1,8) AS session, seq,
                             preceding_prompt FROM v_file_history ORDER BY captured_at"""))
    print("\nTOOLS USED")
    print_rows(db.execute("""SELECT name, count(*) AS uses, sum(coalesce(is_error,0)) AS errors
                             FROM tool_call GROUP BY name ORDER BY uses DESC"""))
    print("\nSESSIONS WITH NO CODE CHANGE (often pure Q&A: good knowledge candidates)")
    print_rows(db.execute("""SELECT substr(session_id,1,8) AS session, developer_email, message_count AS msgs,
                             (SELECT text FROM message m WHERE m.session_id = s.session_id
                               AND m.kind = 'prompt' ORDER BY seq LIMIT 1) AS first_prompt
                             FROM session s
                             WHERE message_count > 0 AND NOT EXISTS
                               (SELECT 1 FROM snapshot sn JOIN file_change fc ON fc.snapshot_commit = sn.commit_id
                                 WHERE sn.session_id = s.session_id AND sn.is_baseline = 0)"""))
    db.close()


def export(args):
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    s = db.execute("SELECT * FROM session WHERE session_id LIKE ?", (args.session + "%",)).fetchone()
    if not s:
        sys.exit(f"No session starting with {args.session}")
    sid = s["session_id"]
    out = [f"# Session {sid}", "",
           f"- **Developer:** {s['developer_email'] or 'unknown'}",
           f"- **Repository:** {s['repository'] or 'unknown'} (branch {s['branch'] or '?'})",
           f"- **Started from commit:** {s['start_commit'] or 'unknown'}",
           f"- **Time:** {s['started_at'] or '?'} to {s['ended_at'] or '?'}",
           f"- **Capture ID:** {s['capture_id'] or 'unknown'}", ""]
    items = []
    for m in db.execute("SELECT * FROM message WHERE session_id = ? AND kind IN ('prompt','response')", (sid,)):
        if m["text"].strip():
            items.append((m["timestamp"] or "", m["seq"], "msg", m))
    for t in db.execute("SELECT * FROM tool_call WHERE session_id = ?", (sid,)):
        items.append((t["timestamp"] or "", t["seq"], "tool", t))
    for sn in db.execute("SELECT * FROM snapshot WHERE session_id = ?", (sid,)):
        items.append((sn["captured_at"] or "", 10 ** 9, "snap", sn))
    for _, _, kind, r in sorted(items, key=lambda x: (x[0], x[1])):
        if kind == "msg":
            who = "Developer" if r["role"] == "user" else "Claude"
            sub = " (sub-agent)" if r["is_sidechain"] else ""
            out += [f"## {who}{sub}", "", r["text"].strip(), ""]
        elif kind == "tool":
            target = r["file_path"] or (r["command"] or "")[:200]
            status = "failed" if r["is_error"] else "ok"
            out += [f"> Tool **{r['name']}** `{target}` ({status})", ""]
        else:
            changes = db.execute("SELECT change_type, path, lines_added, lines_removed FROM file_change "
                                 "WHERE snapshot_commit = ?", (r["commit_id"],)).fetchall()
            if r["is_baseline"]:
                out += [f"### Starting state ({r['commit_id'][:10]}): uncommitted work already present when the session began", ""]
            else:
                out += [f"### Snapshot {r['seq']} ({r['commit_id'][:10]}, {r['captured_at']})", ""]
            out += [f"- {c['change_type']} `{c['path']}` (+{c['lines_added'] or 0} / -{c['lines_removed'] or 0})"
                    for c in changes] or ["- no file changes"]
            if r["excluded"] and r["excluded"].strip() != "none":
                out.append(f"- excluded for secrets: {r['excluded'].strip()}")
            out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    db.close()


def query(args):
    db = sqlite3.connect(args.db)
    print_rows(db.execute(args.sql), max_width=100)
    db.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Build and explore the structured capture index.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="rebuild the index from the outbox and capture projects")
    b.add_argument("--outbox", default=str(DEFAULT_OUTBOX))
    b.add_argument("--capture", action="append", default=[],
                   help="capture project, or folder of capture projects (repeatable)")
    b.add_argument("--state", default=str(DEFAULT_STATE))
    r = sub.add_parser("report", help="print an overview with sample queries")
    e = sub.add_parser("export", help="write one session as readable Markdown")
    e.add_argument("session", help="session ID (the first few characters are enough)")
    e.add_argument("--out")
    q = sub.add_parser("query", help="run your own SQL against the index")
    q.add_argument("sql")
    for p in (b, r, e, q):
        p.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    {"build": build, "report": report, "export": export, "query": query}[args.cmd](args)


if __name__ == "__main__":
    main()
