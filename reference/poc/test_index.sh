#!/usr/bin/env bash
# End-to-end test of kb_index.py: real snapshots from capture.sh plus transcripts in
# Claude Code's JSON-lines format, then build, report, export and layer-3 persistence.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CAP="$HERE/capture.sh"; IDX="python3 $HERE/kb_index.py"
T="$(mktemp -d)"
export CAPTURE_HOME="$T/capture-home" CAPTURE_SYNC=1 CAPTURE_REMOTE_BASE="$T/group"
DB="$T/index.db"
PASS=0; FAIL=0
check(){ if eval "$2"; then echo "  PASS  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
sql(){ python3 -c "import sqlite3,sys; print(sqlite3.connect('$DB').execute(sys.argv[1]).fetchone()[0])" "$1"; }
hook(){ printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s","hook_event_name":"%s"}' \
          "$2" "$T/t-$2.jsonl" "$3" "$1" | "$CAP" "$1"; }
now(){ python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]+"Z")'; }
line(){ echo "$1" >> "$T/t-$2.jsonl"; sleep 1; }

mkdir -p "$T/group"; git init -q --bare "$T/group/billing-app-capture.git"
git init -q --bare "$T/billing-app.git"
git init -q -b main "$T/billing-app"; cd "$T/billing-app"
git config user.email dev@example.com; git config user.name Dev
echo "old notes" > notes.txt; git add -A; git commit -qm initial
git remote add origin "$T/billing-app.git"; git push -q origin main

echo "Session A: code changes, a deletion and a sub-agent"
A="aaaa1111-0000-0000-0000-000000000001"; : > "$T/t-$A.jsonl"
MARK="$(hook session-start "$A" "$T/billing-app" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')"
line "$(python3 -c 'import json,sys;print(json.dumps({"type":"attachment","sessionId":sys.argv[1],"timestamp":sys.argv[3],"attachment":{"type":"hook_additional_context","content":[sys.argv[2]]}}))' "$A" "$MARK" "$(now)")" "$A"
line '{"type":"user","uuid":"a-u1","parentUuid":null,"sessionId":"'$A'","timestamp":"'$(now)'","gitBranch":"main","message":{"role":"user","content":"Add a function to compute invoice totals with tax"}}' "$A"
line '{"type":"assistant","uuid":"a-a1","parentUuid":"a-u1","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"I will create invoice.py."},{"type":"tool_use","id":"toolu_write1","name":"Write","input":{"file_path":"'$T'/billing-app/invoice.py","content":"def total(x, tax): return x * (1 + tax)\n"}}]}}' "$A"
printf 'def total(x, tax):\n    return x * (1 + tax)\n' > invoice.py
line '{"type":"user","uuid":"a-u2","parentUuid":"a-a1","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_write1","content":"File created successfully"}]}}' "$A"
line '{"type":"assistant","uuid":"a-a2","parentUuid":"a-u2","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Created invoice.py with a total() function."}]}}' "$A"
hook snapshot "$A" "$T/billing-app"
line '{"type":"user","uuid":"a-u3","parentUuid":"a-a2","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"user","content":"Remove the old notes file"}}' "$A"
line '{"type":"assistant","uuid":"a-a3","parentUuid":"a-u3","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bash1","name":"Bash","input":{"command":"rm notes.txt"}}]}}' "$A"
rm notes.txt
line '{"type":"user","uuid":"a-u4","parentUuid":"a-a3","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bash1","content":"","is_error":false}]}}' "$A"
line '{"type":"assistant","uuid":"a-s1","parentUuid":"a-u4","isSidechain":true,"sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Sub-agent: no other references to notes.txt found."}]}}' "$A"
line '{"type":"assistant","uuid":"a-a4","parentUuid":"a-s1","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Deleted notes.txt."}]}}' "$A"
line '{"type":"summary","summary":"Invoice totals and cleanup","leafUuid":"a-a4"}' "$A"
hook session-end "$A" "$T/billing-app"

echo "Session B: a question with no code change"
B="bbbb2222-0000-0000-0000-000000000002"; : > "$T/t-$B.jsonl"
hook session-start "$B" "$T/billing-app" >/dev/null
line '{"type":"user","uuid":"b-u1","parentUuid":null,"sessionId":"'$B'","timestamp":"'$(now)'","message":{"role":"user","content":"How do I configure the proxy for the billing API?"}}' "$B"
line '{"type":"assistant","uuid":"b-a1","parentUuid":"b-u1","sessionId":"'$B'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Set HTTPS_PROXY before starting the service."}]}}' "$B"
hook session-end "$B" "$T/billing-app"

echo; echo "Build"
$IDX build --db "$DB" --capture "$T/group"
check "two sessions indexed" "[ \"\$(sql 'select count(*) from session')\" = 2 ]"
check "marker linked: developer, repository, start commit" "[ \"\$(sql \"select developer_email||'|'||repository||'|'||start_commit from session where session_id='$A'\")\" = \"dev@example.com|billing-app|\$(git rev-parse HEAD)\" ]"
check "session title from summary" "[ \"\$(sql \"select title from session where session_id='$A'\")\" = 'Invoice totals and cleanup' ]"
check "prompts and responses classified (2 prompts, 5 responses)" "[ \"\$(sql \"select count(*) from message where session_id='$A' and kind in ('prompt','response')\")\" = 7 ]"
check "sub-agent message flagged" "[ \"\$(sql \"select is_sidechain from message where uuid='a-s1'\")\" = 1 ]"
check "Write tool call with file path and result" "[ \"\$(sql \"select name||'|'||(file_path like '%invoice.py')||'|'||result_text from tool_call where tool_use_id='toolu_write1'\")\" = 'Write|1|File created successfully' ]"
check "Bash tool call with command" "[ \"\$(sql \"select command from tool_call where tool_use_id='toolu_bash1'\")\" = 'rm notes.txt' ]"
check "two snapshots for session A" "[ \"\$(sql \"select count(*) from snapshot where session_id='$A'\")\" = 2 ]"
check "invoice.py recorded as added (+2 lines)" "[ \"\$(sql \"select change_type||lines_added from file_change where path='invoice.py'\")\" = added2 ]"
check "notes.txt recorded as deleted" "[ \"\$(sql \"select change_type from file_change where path='notes.txt'\")\" = deleted ]"
check "change linked to the prompt that caused it" "[ \"\$(sql \"select preceding_prompt from v_file_history where path='invoice.py'\")\" = 'Add a function to compute invoice totals with tax' ]"
check "deletion linked to its prompt" "[ \"\$(sql \"select preceding_prompt from v_file_history where path='notes.txt'\")\" = 'Remove the old notes file' ]"
check "Q&A session credited with no changes" "[ \"\$(sql \"select snapshot_count from session where session_id='$B'\")\" = 0 ]"
check "Q&A session keeps a baseline of the pre-existing uncommitted work" "[ \"\$(sql \"select count(*) from snapshot where session_id='$B' and is_baseline=1\")\" = 1 ]"
check "Q&A session listed as a knowledge candidate" "python3 $HERE/kb_index.py report --db $DB | grep -A3 'NO CODE CHANGE' | grep -q 'proxy'"
check "branch backup indexed" "[ \"\$(sql 'select count(*) from branch_backup')\" -ge 1 ]"

echo; echo "Report"
$IDX report --db "$DB"
echo; echo "Export"
$IDX export aaaa1111 --db "$DB" --out "$T/session.md"
cat "$T/session.md"
check "export contains conversation, tool calls and snapshots" "grep -q 'Add a function' $T/session.md && grep -q 'Tool \*\*Write\*\*' $T/session.md && grep -q 'deleted .notes.txt' $T/session.md"

echo; echo "Same snapshots loaded from two places"
$IDX build --db "$DB" --capture "$T/group" --capture "$T/billing-app" >/dev/null
check "no duplicate file changes" "[ \"\$(sql \"select count(*) from file_change where path='invoice.py' and snapshot_commit in (select commit_id from snapshot where session_id='$A')\")\" = 1 ]"

echo; echo "Layer 3 survives a rebuild"
python3 -c "import sqlite3; c=sqlite3.connect('$DB'); c.execute(\"insert into knowledge_entry(entry_type,title) values('answer','Configure the billing API proxy')\"); c.execute(\"insert into evidence_link values(1,'session','$B')\"); c.commit()"
$IDX build --db "$DB" --capture "$T/group" >/dev/null
check "knowledge entry and its evidence link kept" "[ \"\$(sql \"select k.title from knowledge_entry k join evidence_link e on e.entry_id=k.entry_id join session s on s.session_id=e.evidence_key\")\" = 'Configure the billing API proxy' ]"

echo; echo "Result: $PASS passed, $FAIL failed"
rm -rf "$T"; [ "$FAIL" -eq 0 ]
