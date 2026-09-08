# Blackink docs

Reorganised 2026-09-03. Five active documents. **v2 documents are
authoritative** — anything contradicting them is stale.

## Active

| # | File | What it is |
|---|---|---|
| 1 | **[CLAUDE_heu.md](CLAUDE_heu.md)** | Project context, hard invariants, what's cancelled, Week 1 split, exact event names. **Start here** |
| 2 | **[Week1_Tasks_Dev_Split_v2.md](Week1_Tasks_Dev_Split_v2.md)** | **The** Week 1 sprint plan. All four devs, subtasks, business requirements, Definitions of Done, shared Sept 11 gate. Active scope only |
| 3 | **[Implementation_Blueprint_v2.md](Implementation_Blueprint_v2.md)** | Full system spec: data contracts, architectural invariants, Weeks 0–4, commercial matrix, nine-agent architecture |
| 4 | [reuse_ledger_week0.md](reuse_ledger_week0.md) | Forced Action reuse audit — direct port / refactor / pattern-only. Maintaining this is a standing client requirement |

Plus **`.scratch/task-3.1-outbound-sequencer/`** (outside `docs/`) — the Dev 3
Task 3.1 decision map. `MAP.md` records every architectural decision and why,
`CLIENT-ASKS.md` tracks open client dependencies, `SENDING-DOMAINS.md` holds
the domain inventory. **Read `MAP.md` before implementing any part of Task
3.1.**

And **`../CLAUDE.md`** — repo conventions, tenant-isolation rules, migration
order, tooling.

## archive/

Superseded or historical. Kept for the reasoning trail and provenance.
**Do not build from these.**

| File | Why archived |
|---|---|
| `Blackink_Dev3_Task_3.1_Complete_Reference.md` | Superseded by `Week1_Tasks_Dev_Split_v2.md`. Built Touch 1 on the ghost-shopper audit and Sendspark video, both cancelled |
| `Blackink_Source_of_Truth.md` | 337 KB compilation of the four original client documents plus full raw source. **v2 is this document already applied.** Consult only to trace *why* a decision was made, or for the decision register (§5.1) of genuinely unresolved items — O-05/O-06 still block 3.1.3 |
| `SOURCE_OF_TRUTH_RECONCILIATION.md` | Mapped the Source of Truth onto this repo's code. Scope conclusions superseded by v2; its repo-level findings were carried into the Task 3.1 ticket map |
| `brief.md` | v1 blueprint summary — still metro-based, still lists Sendspark/Twilio/Calendly |
| `week0_sprint.md` | Week 0 complete |

## Rule

**One active document per topic.** When a document is replaced, move the old
one to `archive/` and add a row above — never leave two live versions of the
same plan in this folder.
