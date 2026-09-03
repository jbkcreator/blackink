# Where does the sequencer live, and how do per-contact due times fire?

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03)

**Seat:** the sequencer is an `agent_work_orders` producer/consumer, not a
standalone service. The seam was reserved for it by name.

**Scheduling:** day-grain tolerance, so no real scheduler — the existing
`while true; sleep N` compose idiom suffices. Add a **`due_batch()`** that
selects `WHERE status='QUEUED' AND due_at <= now()`, using the existing
`ix_awo_due ON (status, due_at)` index. **Do not reuse SNOOZED** for scheduled
touches: snooze means *a human deferred this*, and conflating it with *the
system scheduled this* corrupts the audit trail for the sake of ~15 lines.

Consequence: `enqueue()` for a future touch sets `due_at`, and the card is
**not posted until the touch is due**. Today nothing gates card posting on
`due_at`, so that gate is part of this work — otherwise a Day 10 touch posts
its approval card on Day 0 and sits in `#blackink-command` for a fortnight.

**Operating it is ours.** Add compose services for the work-order sweep and
the Cora worker. Nothing runs them today. Match the existing idiom and pick
intervals against day-grain tolerance — the sweep does not need to be tight;
`promotion-sweep`'s 300s is a reasonable precedent, and there is no benefit to
going below a minute.

**Idempotency key:** the sequencer must supply its own natural key —
contact + touch step + sequence run. The default helper is **hour-bucketed**
(`sha256(client_id|entity_id|action_class|hour)`), which would re-enqueue the
same touch every hour.

**Send window:** business-hours sending is a new decision, not a ground-truth
requirement — CLIENT-ASKS **D14**.

**Spun out:** the `reclaim_stale_executing` double-execute risk is now
ticket [22](22-execution-lease-double-send.md). It is a **blocker** for live
email and must land before the real dispatcher registers.

**Reported to Dev 2, not fixed here:** `notify_approval_resolved()` has no
caller (Cora's pending counter only climbs, so it auto-pauses at 50 drafts and
never resumes), and W0-1's 24-hour aged-draft trigger does not exist despite
`queued_depth(older_than=)` being written for it.

**Still to decide in ticket 07:** whether halt is also checked at enqueue.
Today it is execution-only, so a halted client still accumulates QUEUED rows
and posts cards.

## Question

Two entangled questions that must be answered together.

**1. Is the sequencer an agent or a service?**

The blueprint (§23) names the Campaign Agent as Cora + Relay: Cora drafts,
Relay executes with idempotency, rate throttling, and circuit-breaker halts.
The repo already has both, plus `agent_work_orders` — which supplies
idempotency keys, `due_at`, autonomy bands, risk classes, execution receipts,
and hash-bound Slack approval cards. The blueprint's Band 1/2/3 autonomy
language (§23) maps one-to-one onto the existing `autonomy_band` column,
which strongly suggests the sequencer was meant to be an agent.

Against that: `cora/worker.py:_process_draft()` is an explicit placeholder
whose docstring says real drafting "belongs to the outreach-pipeline
workstream" — i.e. this task. And **no compose service runs the Cora, Relay,
or work-order workers at all**, so adopting the framework means also making
it actually run.

So: does each touch become an `agent_work_orders` row, or is the sequencer a
standalone service plus a scheduled task, with the agent framework left
alone?

**2. How does "Touch 3 is due at T+4 days for contact N" actually fire?**

There is no cron, no APScheduler, no Celery. `docker-compose.yml` runs
`sh -c "while true; do python -m src.tasks.X; sleep N; done"` — a fleet-sweep
pattern that suits `promotion_sweep`, but does not naturally express a
per-contact due time.

Options: a due-time sweeper over `agent_work_orders.due_at` (the column
exists; nothing sweeps it); a new sequencer sweep task in the same
`while/sleep` idiom querying sequence state for due touches; Redis sorted-set
delayed queue via `tenant_redis`; or introducing a real scheduler.

Settle also: what is the acceptable **granularity and lateness**? A day-grain
touch tolerates a slow sweep. 3.1.2's "card within 60 seconds of Touch 1
dispatch" (§11.2) does not — and that constraint may force an event-driven
path alongside the sweep. Note ticket 14 disputes that 60s figure.

And: **idempotency**. What stops a touch being sent twice if a sweep overlaps
its predecessor, or a worker dies mid-dispatch?

## Update from the Source of Truth (2026-09-03)

**Question 1 is close to decided, and not by us.** §2.7 and §1.6 require
**every early-client send to be human-reviewed** before dispatch. A sequencer
that must produce an approvable draft, park it, and dispatch on approval is
almost exactly what `agent_work_orders` plus the existing hash-bound Slack
approve/reject listeners already do. Reimplementing that as a standalone
service would duplicate the approval seam, the idempotency keys, and the halt
integration.

Ticket 21 owns the approval workflow itself; this ticket should now treat
"agent work orders" as the default and require a positive argument to depart
from it.

Two further constraints this adds to question 2:

- **The approval queue is a second source of delay.** A touch due on Day 4
  may sit unapproved. The scheduler must distinguish "not yet due", "due and
  awaiting approval", and "approved, awaiting dispatch" — three states, not
  two. Ticket 07's state model has to carry that.
- **Relay's halt is absolute**: "a TTL or restart must never re-arm a paused
  campaign… An operational lease TTL is not permission to expire a safety
  halt" (§1.4). Whatever fires due touches must check halt state at dispatch,
  not only at enqueue.

Also from §2.3: the Day 0/1–2/4/7/10 offsets are a "**configurable** starting
cadence", not constants. Whatever fires touches reads the offsets from
configuration.

And a caution on the existing lease code — §5.2 reports the printed
`EntityLeaseManager` has a non-atomic get/compare/delete release, uses an
agent name as a lease token (not unique), and can let TTL expiry admit a
second worker while the first is live. Verify the repo's version before
relying on it for dispatch idempotency.

## Resolution in progress (2026-09-03)

### Q1 — the seat: **`agent_work_orders`, decided**

The seam was reserved for this task by name. `action_class` defaults to
`DISPATCH_EMAIL_TOUCH` (`work_orders/__main__.py:207`); the dispatcher
registry holds only `noop`, whose docstring says the real email dispatcher
"lands with the Week 1 Campaign Agent"; and `__main__.py:19` states the runner
does not change when it registers.

What Task 3.1 inherits working: hash-bound Slack approval cards with integrity
and staleness rejection; `record_decision` guarded against double-clicks;
`UNIQUE (client_id, idempotency_key)` with enqueue-on-conflict returning the
existing row; atomic `claim_for_execution`; `autonomy_band` / `risk_class` as
real CHECK-constrained columns; and correct tenancy — `agent_work_orders` is
in `TENANT_POLICIES`, filters `client_id` explicitly *and* relies on RLS.

Approval and execution are already separate passes, which is exactly the shape
ticket 21's every-send review needs: the Slack listener flips QUEUED→APPROVED
and executes nothing; a sweep then claims APPROVED rows and dispatches.

### Q2 — scheduling: day-grain, so no real scheduler needed

**Tolerance is day-grain** (decided; cross-checked — the ground truth has no
sub-day requirement for cold touches; the only sub-day numbers are Touch 2's
disputed 60 seconds and the Respond *inbound* SLA). So the existing
`while true; sleep N` idiom in `docker-compose.yml` is sufficient. No cron,
APScheduler or Celery.

**The due-time mechanism already exists, under another name.** `due_at` is
inert for QUEUED rows — nothing reads it, so a work order enqueued for Day 4
posts its card on Day 0. But `snooze()` sets `due_at` and moves
QUEUED→SNOOZED, and `requeue_due_snoozed()` flips SNOOZED→QUEUED once due.
That is precisely "wake this on Day 4", already written, already served by the
`ix_awo_due ON (status, due_at)` index.

Two options:

- **Reuse SNOOZED** — zero new code, but "snoozed" means *a human deferred
  this*, which a scheduled future touch is not. Misleads the audit trail.
- **Add `due_batch()` for QUEUED** — ~15 lines, reads `due_at` the way the
  existing index clearly anticipates, keeps SNOOZED meaning human-deferred.

Recommended: the second. Conflating "the system scheduled this" with "a person
postponed this" will cost more later than the lines saved. **Awaiting
confirmation.**

**Send window:** business-hours sending is a *new decision*, not a ground-truth
requirement — see CLIENT-ASKS **D14**, which also flags that calendar-day
offsets plus business-hours-only sending yields business-day behaviour
sideways, and that all ten launch counties are Eastern so timezone can be
deferred.

## ⛔ Blocker before this framework carries live email

`reclaim_stale_executing` recovers dead workers on a **30-minute wall clock
with no lease and no heartbeat** — self-flagged in the code. A dispatch
exceeding 30 minutes is reclaimed and **executed again**. Harmless for `noop`;
for SMTP it means **the prospect receives the same cold email twice**, and it
cannot be un-sent.

Must be fixed before the real dispatcher registers: a lease with heartbeat, or
a send-side idempotency check against the dispatch record. Not Task 3.1's
code, but Task 3.1 is what makes it dangerous. **Ownership unresolved.**

## Other gaps inherited

- **Nothing runs.** No compose service executes the work-order sweep, the Cora
  worker, or Relay — only `api`, `promotion-sweep` (300s),
  `deliverability-sentinel` (3600s), `county-allocation` (86400s). Adopting the
  framework means also operating it. **Ownership unresolved.**
- **Default idempotency key is hour-bucketed** —
  `sha256(client_id|entity_id|action_class|hour)`. That would let the same
  touch re-enqueue hourly. The sequencer **must** pass its own natural key:
  contact + touch step (+ sequence run).
- **Halt is checked at execution only, never at enqueue.** A halted client
  still accumulates QUEUED rows and posts approval cards. Given §1.4's "a TTL
  or restart must never re-arm a paused campaign", consider checking at both.
- **Two Dev 2 bugs to report, not fix quietly:**
  `notify_approval_resolved()` has **no caller**, so Cora's pending counter
  only climbs — it auto-pauses at 50 drafts and never resumes. And W0-1's
  24-hour aged-draft trigger does not exist, though `queued_depth(older_than=)`
  is already written and documented as its reader.

**Correction to an earlier note in this ticket:** Cora's worker is
**synchronous** — a `while` loop with `time.sleep`, no asyncio anywhere. The
concern about sync `log_event` blocking an event loop was wrong; there is no
event loop.

## Why it matters

This choice determines whether Task 3.1 inherits halts, autonomy bands, and
approval cards for free, or reimplements them.
