#!/usr/bin/env bash
# claude-capture POC: Claude Code hook script.
#
# Usage (wired up in Claude Code settings, see settings.example.json):
#   capture.sh session-start   # SessionStart hook
# Requires bash (Git Bash on Windows), git and Python (python3, python or py).
#   capture.sh snapshot        # Stop hook (after each Claude response)
#   capture.sh session-end     # SessionEnd hook
#
# Claude Code passes the hook input as JSON on stdin
# (session_id, transcript_path, cwd, hook_event_name, ...).
#
# Configuration (environment variables):
#   CAPTURE_REMOTE         Git URL of the capture project for this repository.
#   CAPTURE_REMOTE_BASE    Alternative: base URL of the capture group; the capture project is
#                          derived as <base>/<repository name>-capture.git
#                          (e.g. git@gitlab.example.com:claude-capture).
#                          Neither set = keep snapshots local only.
#   CAPTURE_HOME           State, logs and outbox. Default: ~/.claude-capture
#   CAPTURE_OUTBOX         Where transcripts are "uploaded". Default: $CAPTURE_HOME/outbox
#                          (stands in for the ingestion endpoint in this POC)
#   CAPTURE_SYNC=1         Push synchronously (used by the test). Default: background push.
#
# Design rules:
#   - Never block or fail the developer's session: every path exits 0.
#   - Never touch the developer's branch, HEAD, index (staged changes) or files.
#   - Respect .gitignore; scan for secrets before anything leaves the machine.

set -u

MODE="${1:-}"
CAPTURE_HOME="${CAPTURE_HOME:-$HOME/.claude-capture}"
CAPTURE_OUTBOX="${CAPTURE_OUTBOX:-$CAPTURE_HOME/outbox}"
CAPTURE_REMOTE="${CAPTURE_REMOTE:-}"
CAPTURE_REMOTE_BASE="${CAPTURE_REMOTE_BASE:-}"
REF_NS="refs/claude-capture"
LOG="$CAPTURE_HOME/capture.log"

mkdir -p "$CAPTURE_HOME/state" "$CAPTURE_OUTBOX" 2>/dev/null

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$*" >> "$LOG" 2>/dev/null; }
finish() { exit 0; }

INPUT="$(cat 2>/dev/null || true)"

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json' >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && { log "no working Python found (tried python3, python, py); nothing captured"; exit 0; }

json_get() {
  # json_get <key>  -> prints value of a top-level key from the hook input, or empty
  INPUT_JSON="$INPUT" "$PY" -c '
import json, os, sys
try:
    d = json.loads(os.environ.get("INPUT_JSON") or "{}")
    v = d.get(sys.argv[1], "")
    print(v if isinstance(v, str) else json.dumps(v))
except Exception:
    print("")
' "$1" 2>/dev/null
}

SESSION_ID="$(json_get session_id)"
TRANSCRIPT="$(json_get transcript_path)"
CWD="$(json_get cwd)"
EVENT="$(json_get hook_event_name)"
[ -z "$SESSION_ID" ] && { log "no session_id in hook input; nothing to do"; finish; }
[ -z "$CWD" ] && CWD="$PWD"
TRANSCRIPT="${TRANSCRIPT//\\//}"; CWD="${CWD//\\//}"

STATE="$CAPTURE_HOME/state/$SESSION_ID"
mkdir -p "$STATE"
[ -n "$TRANSCRIPT" ] && echo "$TRANSCRIPT" > "$STATE/transcript_path"

# ---------------------------------------------------------------- helpers

in_repo() { git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; }

resolve_remote() {
  # Use CAPTURE_REMOTE, or derive the capture project from CAPTURE_REMOTE_BASE.
  [ -n "$CAPTURE_REMOTE" ] && return 0
  [ -n "$CAPTURE_REMOTE_BASE" ] && in_repo || return 0
  local origin name
  origin="$(git -C "$CWD" remote get-url origin 2>/dev/null || git -C "$CWD" rev-parse --show-toplevel)"
  name="$(basename "$origin")"; name="${name%.git}"
  CAPTURE_REMOTE="${CAPTURE_REMOTE_BASE%/}/${name}-capture.git"
}

repo_root() { git -C "$CWD" rev-parse --show-toplevel 2>/dev/null; }

user_slug() {
  local e
  e="$(git -C "$CWD" config user.email 2>/dev/null || whoami)"
  printf '%s' "$e" | tr -c 'A-Za-z0-9._-' '_'
}

# Built-in secret patterns; gitleaks is used instead when installed.
SECRET_RE='AKIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|(api[_-]?key|secret|token|passw(or)?d)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9/+_.-]{12,}'

file_has_secret() {
  # file_has_secret <path>  -> exit 0 if a secret is detected
  local f="$1"
  [ -f "$f" ] || return 1
  if command -v gitleaks >/dev/null 2>&1; then
    gitleaks detect --no-git --no-banner --source "$f" >/dev/null 2>&1 && return 1 || return 0
  fi
  grep -I -E -q -i -- "$SECRET_RE" "$f" 2>/dev/null
}

push_ref() {
  # push_ref <local ref> [force]  -> push one ref to the capture project, queue on failure
  local ref="$1" force="${2:-}"
  local root; root="$(repo_root)"
  if [ -z "$CAPTURE_REMOTE" ]; then
    log "CAPTURE_REMOTE not set; kept $ref locally"
    return 0
  fi
  local spec="$ref:$ref"; [ -n "$force" ] && spec="+$spec"
  if [ "${CAPTURE_SYNC:-}" = "1" ]; then
    git -C "$root" push -q "$CAPTURE_REMOTE" "$spec" >/dev/null 2>&1 \
      && log "pushed $ref" \
      || { echo "$root|$spec" >> "$CAPTURE_HOME/pending"; log "push failed, queued $ref"; }
  else
    ( git -C "$root" push -q "$CAPTURE_REMOTE" "$spec" >/dev/null 2>&1 \
        && log "pushed $ref" \
        || { echo "$root|$spec" >> "$CAPTURE_HOME/pending"; log "push failed, queued $ref"; } ) \
      </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi
}

catch_up_transcripts() {
  # The transcript file is written asynchronously and a session can end abruptly,
  # so re-copy the transcripts of earlier sessions to pick up anything missed.
  local d sid path
  for d in "$CAPTURE_HOME"/state/*/; do
    sid="$(basename "$d")"
    [ "$sid" = "$SESSION_ID" ] && continue
    path="$(cat "$d/transcript_path" 2>/dev/null)"
    [ -n "$path" ] && [ -f "$path" ] || continue
    mkdir -p "$CAPTURE_OUTBOX/$sid"
    if ! cmp -s "$path" "$CAPTURE_OUTBOX/$sid/transcript.jsonl"; then
      cp -f "$path" "$CAPTURE_OUTBOX/$sid/transcript.jsonl" 2>/dev/null && log "caught up transcript for $sid"
    fi
  done
  return 0
}

flush_pending() {
  [ -s "$CAPTURE_HOME/pending" ] && [ -n "$CAPTURE_REMOTE" ] || return 0
  local tmp="$CAPTURE_HOME/pending.$$"
  mv "$CAPTURE_HOME/pending" "$tmp" 2>/dev/null || return 0
  while IFS='|' read -r root spec; do
    [ -d "$root" ] || continue
    git -C "$root" push -q "$CAPTURE_REMOTE" "$spec" >/dev/null 2>&1 \
      && log "retried and pushed $spec" \
      || echo "$root|$spec" >> "$CAPTURE_HOME/pending"
  done < "$tmp"
  rm -f "$tmp"
}

backup_branches() {
  # Mirror the latest tip of every local branch into a per-user namespace.
  in_repo || return 0
  local root; root="$(repo_root)"
  local ns="$REF_NS/users/$(user_slug)/heads"
  local b
  for b in $(git -C "$root" for-each-ref --format='%(refname:short)' refs/heads/); do
    git -C "$root" update-ref "$ns/$b" "refs/heads/$b" 2>/dev/null || continue
    push_ref "$ns/$b" force
  done
}

upload_transcript() {
  # POC: copy the transcript (and any sub-agent files beside it) to the outbox.
  [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] || { log "no transcript file"; return 0; }
  local dest="$CAPTURE_OUTBOX/$SESSION_ID"
  mkdir -p "$dest"
  cp -f "$TRANSCRIPT" "$dest/transcript.jsonl" 2>/dev/null && log "transcript uploaded to $dest"
  # Sub-agent transcripts: Claude Code may keep them in a folder named after the
  # session next to the main transcript. Layout to be verified in Phase 1a.
  local side="${TRANSCRIPT%.jsonl}"
  [ -d "$side" ] && cp -rf "$side" "$dest/" 2>/dev/null && log "sub-agent files uploaded"
  return 0
}

snapshot() {
  # Snapshot the working tree as a hidden commit, without touching the developer's state.
  in_repo || { log "not a git repository: conversation-only session"; return 0; }
  local root; root="$(repo_root)"
  local head=""; head="$(git -C "$root" rev-parse -q --verify HEAD 2>/dev/null || true)"

  # 1. Private staging area: a temporary index file, never the developer's own.
  local idx; idx="$(mktemp "$CAPTURE_HOME/index.XXXXXX")"
  export GIT_INDEX_FILE="$idx"
  if [ -n "$head" ]; then git -C "$root" read-tree "$head"; else git -C "$root" read-tree --empty; fi
  # 2. Stage every file in the working tree into it; .gitignore is respected.
  git -C "$root" add -A . 2>/dev/null

  # 3. Secret scan of changed files; flagged files are reset to their HEAD version.
  local base="$head"; [ -z "$base" ] && base="$(git -C "$root" hash-object -t tree /dev/null)"
  local excluded="" f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if file_has_secret "$root/$f"; then
      git -C "$root" reset -q "${head:-HEAD}" -- "$f" 2>/dev/null || git -C "$root" rm -q --cached -- "$f" 2>/dev/null
      excluded="$excluded $f"
    fi
  done < <(git -C "$root" diff --cached --name-only --diff-filter=ACMR "$base" 2>/dev/null)

  local tree; tree="$(git -C "$root" write-tree)"
  unset GIT_INDEX_FILE; rm -f "$idx"

  # 4. Skip if nothing changed since this session's last snapshot.
  if [ -f "$STATE/last_tree" ] && [ "$(cat "$STATE/last_tree")" = "$tree" ]; then
    log "working tree unchanged since last snapshot; no snapshot"
    return 0
  fi
  if [ ! -f "$STATE/last_tree" ] && [ -n "$head" ] && [ "$(git -C "$root" rev-parse "$head^{tree}")" = "$tree" ]; then
    log "no uncommitted changes; no snapshot"
    return 0
  fi

  # 5. Unpushed local commits travel with the snapshot: scan them too.
  local nopush=""
  if [ -n "$head" ] && git -C "$root" log -p --format= "$head" --not --remotes 2>/dev/null \
       | grep -I -E -q -i -- "$SECRET_RE"; then
    nopush=1
    log "secret pattern found in unpushed commits; snapshot kept local only"
  fi

  # 6. Create the snapshot commit on top of the developer's current commit.
  local seq=$(( $(cat "$STATE/seq" 2>/dev/null || echo 0) + 1 ))
  local branch; branch="$(git -C "$root" symbolic-ref -q --short HEAD 2>/dev/null || echo detached)"
  local msg
  msg="claude-capture snapshot

session: $SESSION_ID
capture-id: $(cat "$STATE/capture_id" 2>/dev/null || echo unknown)
seq: $seq
event: ${EVENT:-$MODE}
branch: $branch
started-from: ${head:-none}
captured-at: $("$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))')
excluded:${excluded:- none}"
  local commit
  if [ -n "$head" ]; then
    commit="$(printf '%s\n' "$msg" | git -C "$root" commit-tree "$tree" -p "$head")"
  else
    commit="$(printf '%s\n' "$msg" | git -C "$root" commit-tree "$tree")"
  fi
  [ -n "$commit" ] || { log "commit-tree failed"; return 0; }

  # 7. Store it under a hidden ref (not a branch) and push it.
  local ref; ref="$REF_NS/sessions/$SESSION_ID/$(printf '%04d' "$seq")"
  git -C "$root" update-ref "$ref" "$commit"
  echo "$seq" > "$STATE/seq"; echo "$tree" > "$STATE/last_tree"
  log "snapshot $ref -> $commit${excluded:+ (excluded:$excluded)}"
  [ -z "$nopush" ] && push_ref "$ref"
  return 0
}

session_marker() {
  # SessionStart: add a marker to the conversation so the server-side transcript
  # carries the repository, branch and commit the session started from.
  local cid; cid="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
  echo "$cid" > "$STATE/capture_id"
  local repo="none" branch="none" head="none" email="unknown"
  if in_repo; then
    local root; root="$(repo_root)"
    repo="$(git -C "$root" remote get-url origin 2>/dev/null || echo "$root")"
    branch="$(git -C "$root" symbolic-ref -q --short HEAD 2>/dev/null || echo detached)"
    head="$(git -C "$root" rev-parse -q --verify HEAD 2>/dev/null || echo none)"
    email="$(git -C "$root" config user.email 2>/dev/null || echo unknown)"
  fi
  local text="Session capture reference for this conversation: capture_id=$cid repository=$repo branch=$branch commit=$head developer=$email"
  echo "$head" > "$STATE/start_commit"
  log "marker: $text"
  MARKER="$text" "$PY" -c '
import json, os
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": os.environ["MARKER"]}}))'
}

# ---------------------------------------------------------------- main

resolve_remote

case "$MODE" in
  session-start)
    session_marker
    snapshot >/dev/null 2>&1      # baseline: uncommitted work present before Claude starts
    catch_up_transcripts
    flush_pending
    backup_branches
    ;;
  snapshot)
    upload_transcript
    snapshot
    ;;
  session-end)
    upload_transcript
    snapshot
    backup_branches
    ;;
  *)
    log "unknown mode '$MODE'"
    ;;
esac
finish
