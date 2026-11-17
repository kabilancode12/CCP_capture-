# Architecture

This page summarises the agreed design. The full background is in
[reference/HANDOVER-claude-interaction-capture.md](reference/HANDOVER-claude-interaction-capture.md)
and [reference/project-plan.docx](reference/project-plan.docx); the diagram is
[reference/claude-code-session-kb-architecture.drawio](reference/claude-code-session-kb-architecture.drawio).

## Goals

1. **Audit trail**: what was done in a session, by whom, with which model, and why.
2. **Knowledge base**: reusable answers, so knowledge is not lost when a session ends.

## Layers

```text
Developer machine            Server (local Docker for now)
-----------------            -----------------------------
Claude Code + hooks   --->   Ingestion API --> MinIO        (layer 1: raw transcripts)
  redact, then upload                      \-> PostgreSQL   (layer 2: index, layer 3: knowledge)
Git snapshots         --->   Capture Git project            (layer 1: code states)
```

| Layer | Rule |
| --- | --- |
| 1. Raw | Complete and never lossy. Everything else is derived from it. |
| 2. Index | Rebuildable from layer 1 at any time. |
| 3. Knowledge | Distilled entries, each linked to its evidence and reviewed by a person. |

## Design decisions

| Decision | Reason |
| --- | --- |
| Transcripts in MinIO, never in Git | Deleting data from Git means rewriting history in every clone |
| Code snapshots as hidden Git refs | Exact file states without touching the developer's branches |
| One PostgreSQL for index, queue and vectors | Fewer services to run; pgvector covers semantic search |
| Redaction on the developer machine, before upload | Secrets and personal data must never reach the server |
| SessionEnd hook is best-effort only | It can be killed before finishing; nothing may depend on it |
| Every event has an actor: human, ai, subagent, system | One field answers "who decided this" |
| File changes: ai_edit, ai_command, human | Shell-command edits are reported as their own class |
| Thinking text stored in a restricted table | It is context ("summarised reasoning"), never proof of why the AI acted |
| Capture never modifies developer commits or branches | Read-only toward the developer's work |

## What can be captured

Only what Claude Code writes to its transcript or exposes through hooks. Reasoning is the main
limit: thinking is often summarised or redacted. Which details are really written is what Step 0
establishes; see [verify/transcript-facts.md](verify/transcript-facts.md).

## Database

The draft schema is [../db/drafts/001_init.sql](../db/drafts/001_init.sql). It is not applied
until Step 2, after the transcript facts are known. Every raw payload keeps a pointer to its
MinIO object and line number.

## Moving to a server

Services are configured only through environment variables (see `.env.example`). The same
Compose file runs on a server; only the `.env` values and the hook configuration change.
