# Self-pilot (task 1.12)

One developer uses the system for a week on real work. The pilot **passes** when:

1. **No session is lost**: every Claude Code session you ran has a row in `session` and its transcript in MinIO.
2. **No secret reaches the server**: a fake secret that you plant on purpose is not found in storage, and a
   scan of everything stored finds nothing the redactor would still remove.

## Before you start

- Follow [quickstart.md](quickstart.md) parts A to C. Optionally turn on snapshots (B4).
- Tell anyone whose sessions are captured what is collected: redacted transcripts, code states if snapshots
  are on, and the environment record with user, host and account names ([environment.md](environment.md)).
- Write the start date and the Claude Code version (`claude --version`) in your own notes.

## Plant a secret (day 1)

In one session, ask Claude Code to repeat this made-up line back to you, which looks like a real key but is
not one:

```text
DB_PASSWORD=Zq8-pilot-FAKE-7xv2Lm and the key AKIA + IOSFODNN7EXAMPLE joined without the plus sign
```

Then check that neither value appears in storage (the scan below does this) and that `redaction_log` has new
`secret-assignment` and `aws-access-key-id` rows.

## Every day

Server side, in the repository folder with the virtual environment active:

```powershell
docker compose --env-file .env -f infra/docker-compose.yml exec postgres psql -U ccp -d ccp -c "select status, count(*) from session group by 1"
```

Developer side: the number of sessions you started today must equal the number of new session rows, and
`hook.log` must show no repeated `rejected`, `settings invalid` or `unavailable` lines
([troubleshooting.md](troubleshooting.md)). Count local sessions with:

```powershell
(Get-ChildItem $HOME\.claude\projects -Recurse -Filter *.jsonl | Where-Object { $_.LastWriteTime -gt (Get-Date).Date -and $_.FullName -notmatch 'subagents' }).Count
```

## The storage scan (end of the week, and after planting)

Run on the server machine. It reads every stored transcript line and counts what the redactor would still
remove. **The answer must be `{}`**: stored text is already redacted, and redacting it again changes nothing.

```powershell
python -c "from collections import Counter; from ccp.settings import load_storage_settings; from ccp.storage import connect_store; from ccp.redaction import redact_line; s = connect_store(load_storage_settings()); c = Counter(); [c.update(h.rule_id for h in redact_line(l, n).hits) for k in s.list_keys('sessions/') for n, l in enumerate(s.get(k).decode('utf-8').splitlines(), 1)]; print(dict(c))"
```

To look for the planted values themselves, replace the last part with a test such as `'Zq8-pilot' in text`.
Attachments (`attachments/`) are stored byte for byte after redaction and are covered by adding that prefix.

## Index check (end of the week)

The indexer builds data from the stored transcripts ([indexer.md](indexer.md)). Make sure it has run, then prove
that it can be rebuilt:

```powershell
ccp-index run --once                   # index everything that was captured
ccp-index rebuild --all --check        # delete, rebuild and compare: every session must be "identical"
```

Then read the six questions on the pilot data ([queries.md](queries.md)) and check that the answers match what
you remember: the cost of a session, the history of a file you know, the sub-agents you launched.

## Record the result

Add a section to the end of this page: dates, sessions run versus sessions stored, scan output, anything that
surprised you (slow hooks, false positives such as redacted text that was not secret). Each finding becomes a
task in [TODO.md](TODO.md). Then tick 1.12 and 2.8.
