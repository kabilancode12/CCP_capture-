#!/usr/bin/env bash
# End-to-end test of capture.sh. Simulates the JSON that Claude Code sends to hooks.
# Creates everything in a temporary directory; touches nothing else.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CAP="$HERE/capture.sh"
T="$(mktemp -d)"
export CAPTURE_HOME="$T/capture-home" CAPTURE_SYNC=1
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

hook() { # hook <mode> <session> <cwd>
  printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s","hook_event_name":"%s"}' \
    "$2" "$T/transcript-$2.jsonl" "$3" "$1" | "$CAP" "$1"
}

echo "Setting up a developer repo, its main remote and a capture project in $T"
git init -q --bare "$T/main.git"
git init -q --bare "$T/capture.git"
git init -q -b main "$T/repo"; cd "$T/repo"
git config user.email dev@example.com; git config user.name Dev
printf '.env\n*.key\n' > .gitignore
echo 'print("v1")' > app.py
echo 'staged base' > staged.txt
git add -A && git commit -qm "initial" && git remote add origin "$T/main.git" && git push -q origin main
START="$(git rev-parse HEAD)"
export CAPTURE_REMOTE="$T/capture.git"
SID="sess-1111"
echo '{"type":"user","message":"fix the bug"}' > "$T/transcript-$SID.jsonl"

echo; echo "1. Session start"
OUT="$(hook session-start "$SID" "$T/repo")"
check "marker returned as additional context" "echo '$OUT' | grep -q 'capture_id=.*commit=$START'"
check "branch backup pushed to capture project" "git -C $T/capture.git rev-parse -q --verify refs/claude-capture/users/dev_example.com/heads/main >/dev/null"

echo; echo "2. Claude edits files; developer has staged something of their own"
echo 'staged change by developer' > staged.txt && git add staged.txt
echo 'print("v2 - edited by Claude")' > app.py
echo '# notes' > notes.md
echo 'AWS_SECRET=abc' > .env
echo 'API_KEY = "sk_live_0123456789abcdef"' > config.py
STAGED_BEFORE="$(git diff --cached --name-only)"
hook snapshot "$SID" "$T/repo"
S1="refs/claude-capture/sessions/$SID/0001"
check "snapshot 1 pushed to capture project" "git -C $T/capture.git rev-parse -q --verify $S1 >/dev/null"
check "developer HEAD unchanged" "[ \"\$(git rev-parse HEAD)\" = '$START' ]"
check "developer staged changes untouched" "[ \"\$(git diff --cached --name-only)\" = '$STAGED_BEFORE' ]"
check "developer working files untouched" "grep -q v2 app.py && [ -f config.py ]"
check "no new branch created" "[ \"\$(git branch --format='%(refname:short)')\" = 'main' ]"
check "snapshot contains uncommitted Claude edit" "git -C $T/capture.git show $S1:app.py | grep -q v2"
check "snapshot contains new untracked file" "git -C $T/capture.git show $S1:notes.md >/dev/null 2>&1"
check "ignored .env excluded" "! git -C $T/capture.git show $S1:.env >/dev/null 2>&1"
check "file with secret excluded" "! git -C $T/capture.git show $S1:config.py >/dev/null 2>&1"
check "exclusion recorded in snapshot message" "git -C $T/capture.git log -1 --format=%B $S1 | grep -q 'excluded: config.py'"
check "snapshot parent is developer's commit" "[ \"\$(git -C $T/capture.git rev-parse $S1^)\" = '$START' ]"
check "transcript uploaded" "[ -f $CAPTURE_HOME/outbox/$SID/transcript.jsonl ]"

echo; echo "3. Claude responds without changing files"
hook snapshot "$SID" "$T/repo"
check "no duplicate snapshot" "! git -C $T/capture.git rev-parse -q --verify refs/claude-capture/sessions/$SID/0002 >/dev/null"

echo; echo "4. Claude edits again"
echo 'print("v3")' > app.py
hook snapshot "$SID" "$T/repo"
S2="refs/claude-capture/sessions/$SID/0002"
check "snapshot 2 pushed" "git -C $T/capture.git rev-parse -q --verify $S2 >/dev/null"
check "diff between snapshots shows only the new edit" "[ \"\$(git -C $T/capture.git diff --name-only $S1 $S2)\" = 'app.py' ]"

echo; echo "5. Developer commits locally but does not push"
rm -f config.py; git add -A && git commit -qm "local work, not pushed"
LOCAL="$(git rev-parse HEAD)"
echo 'print("v4")' > app.py
hook snapshot "$SID" "$T/repo"
S3="refs/claude-capture/sessions/$SID/0003"
check "snapshot 3 builds on the unpushed commit" "[ \"\$(git -C $T/capture.git rev-parse $S3^)\" = '$LOCAL' ]"
check "unpushed commit now preserved in capture project" "git -C $T/capture.git cat-file -e $LOCAL"
check "unpushed commit still not in main repository" "! git -C $T/main.git cat-file -e $LOCAL 2>/dev/null"

echo; echo "6. Capture project unreachable, then back"
echo 'print("v5")' > app.py
CAPTURE_REMOTE="$T/does-not-exist.git" hook snapshot "$SID" "$T/repo"
S4="refs/claude-capture/sessions/$SID/0004"
check "failed push queued, session not blocked" "grep -q '$S4' $CAPTURE_HOME/pending"
hook session-start "sess-2222" "$T/repo" >/dev/null
check "queued snapshot delivered at next session start" "git -C $T/capture.git rev-parse -q --verify $S4 >/dev/null"

echo; echo "6b. Transcript written late (after the session's last hook)"
echo '{"type":"assistant","message":"late line"}' >> "$T/transcript-$SID.jsonl"
hook session-start "sess-4444" "$T/repo" >/dev/null
check "late transcript lines caught up at next session start" "grep -q 'late line' $CAPTURE_HOME/outbox/$SID/transcript.jsonl"

echo; echo "6c. Capture project derived from CAPTURE_REMOTE_BASE"
git init -q --bare "$T/main-capture.git"
echo 'print("base")' > app.py
CAPTURE_REMOTE= CAPTURE_REMOTE_BASE="$T" hook snapshot "sess-5555" "$T/repo"
check "snapshot pushed to <base>/<repo>-capture.git" "git -C $T/main-capture.git rev-parse -q --verify refs/claude-capture/sessions/sess-5555/0001 >/dev/null"

echo; echo "7. Session end and a session outside any repository"
echo 'print("v6")' > app.py
hook session-end "$SID" "$T/repo"
check "final snapshot pushed" "git -C $T/capture.git rev-parse -q --verify refs/claude-capture/sessions/$SID/0005 >/dev/null"
mkdir -p "$T/not-a-repo"; echo '{}' > "$T/transcript-sess-3333.jsonl"
hook snapshot "sess-3333" "$T/not-a-repo"; RC=$?
check "non-repository session exits cleanly" "[ $RC -eq 0 ]"
check "non-repository session transcript still uploaded" "[ -f $CAPTURE_HOME/outbox/sess-3333/transcript.jsonl ]"

echo; echo "Capture project contents:"
git -C "$T/capture.git" for-each-ref --format='  %(refname)' refs/claude-capture
echo; echo "Result: $PASS passed, $FAIL failed"
rm -rf "$T"
[ "$FAIL" -eq 0 ]
