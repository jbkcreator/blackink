# BLOCKER: stale-executing reclaim can double-send a cold email

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03)

**Semantics: at-most-once.** Never send twice; accept occasionally losing a
touch to a crash. Asymmetric costs — a duplicate cold email is irreversible,
reads as spam, feeds the bounce/complaint rates the sentinel quarantines on
(>3% / >0.08% rolling 48h), and Link 2 is judged on bounce under 3%. A missed
touch costs one prospect one email out of five, and the sequence continues.

**The guarantee is a unique constraint, not a lease.** Per §2.7 — "a lease
prevents contradictory concurrent work; **it does not itself prove a send is
unique**" — and §5.2's demand to "preserve idempotency explicitly" rather than
rely on a non-unique index.

`UNIQUE (sequence_run_id, touch_step)` on the per-touch dispatch record in
ticket 07's `sequence_runs` model. Dispatch becomes:

1. **INSERT** the dispatch row with `status='SENDING'`. A unique violation
   means this touch is already claimed or sent — **abort, do not send.**
2. **Send.**
3. **UPDATE** to `status='SENT'` with the returned `Message-ID`.

The database is the guarantee; timing is irrelevant to correctness.

**Ambiguous rows alert, they do not auto-resolve.** A row stuck in `SENDING`
genuinely might or might not have transmitted. Alert to `#blackink-qa` for
human review rather than guessing — matching §2.8's rule that stale or missing
required data returns `UNKNOWN`/`ABSTAIN` and "cached degraded results are
labelled, never falsely fresh."

**Do not shorten `STALE_EXECUTING_AFTER`.** Counter-intuitively, a shorter
reclaim window reclaims *live* dispatches more often and makes duplicates more
likely. Dev 2's "generous on purpose" comment is correct. Keep 30 minutes; let
the constraint do the work. A lease/heartbeat becomes optional — useful for
observability, unnecessary for safety.

---

## Bug A — unchecked `record_execution_result` return (OURS)

**Ownership:** `src/services/work_orders/` was introduced in `3cec292`
("Week 0 — Dev 3 complete: Slack Agent Hub") and `CLAUDE.md` annotates
`migrations/apply_agent_work_orders.py` with `# Dev 3`. **This is Dev 3's own
module — fix it, do not file it.**

**Severity:** currently harmless (`noop` is the only dispatcher). Becomes a
silent duplicate-send the moment ticket 05's SMTP dispatcher registers.

**The path:**

- `__init__.py:468-492` — `record_execution_result` is guarded
  `WHERE status = 'EXECUTING'` and returns `None` when `rowcount == 0`.
- `__main__.py:193` — `wo.record_execution_result(..., success=True, receipt=receipt)`
  **discards the return value** and logs `DONE` unconditionally.
- `__main__.py:188` — the failure path discards it too.

**Reproduction:**

1. `claim_for_execution` (`__init__.py:444`) commits `APPROVED → EXECUTING`
   and stamps `updated_at`. Nothing renews that stamp afterwards — this is the
   missing heartbeat, in one line.
2. The dispatcher runs longer than `STALE_EXECUTING_AFTER` (30 min).
3. A concurrent sweep's `reclaim_stale_executing` (`__init__.py:413`) flips the
   row `EXECUTING → APPROVED`.
4. The original worker finishes; `record_execution_result` matches no
   `EXECUTING` row, returns `None`.
5. `__main__.py:193` ignores it and prints `DONE`.
6. The next sweep sees `APPROVED` and dispatches again.

**Net effect:** the email is sent twice, the first receipt **and its
`Message-ID` are silently discarded**, and the log claims success. The
discarded `Message-ID` is itself a second failure — Touch 3's threading DoD
depends on persisting Touch 1's.

**Fix:** check the return. `None` from `record_execution_result` means the row
was reclaimed mid-flight — log at ERROR, alert `#blackink-qa`, and do **not**
report `DONE`. Same on the failure path at `:188`. Three lines, and it turns a
silent race into a visible one. The unique constraint above then prevents the
re-dispatch entirely; this fix makes the near-miss observable.

**Test:** hold a stub dispatcher open past the reclaim window and assert
exactly one transmission plus one alert.

---

## Bug B — `notify_approval_resolved()` is never called (OURS)

**Ownership:** `src/agents/cora/throttle.py:20` states it explicitly —
"**Dev 3 (Slack bot) calls `notify_approval_resolved()`** when an operator
clicks Approve/Reject on a draft card." Dev 2 wrote and tested the function;
the call site was handed to Dev 3 and never wired.

`notify_draft_queued()` **is** called (`worker.py:64`), so
`cora:approval:pending` only ever increments. At
`DRAFT_QUEUE_CAPACITY = 50` (`throttle.py:33`) Cora auto-pauses, and because
nothing decrements toward `RESUME_THRESHOLD = 40` (`:34`), **it never
resumes.**

Wire it into the Slack approve/reject listener. Belongs to ticket 21's
approval workflow. Note the counter is deliberately **not tenant-scoped**
(`throttle.py:16-18`) — a global platform resource for Week 0.

---

## Bug C — 24-hour aged-draft trigger missing (FILE TO DEV 2)

**The one genuinely external item.** W0-1 (Source of Truth §1.4) requires Cora
to pause and requeue at "**50 unreviewed drafts *or* any unreviewed draft
older than 24 hours**", and names the aged trigger as separately provable at
low volume.

`throttle.py` implements only the count half. The age half needs
`wo.queued_depth(client_id, older_than=timedelta(hours=24))`
(`work_orders/__init__.py:332`) — which **already exists, is tested**
(`tests/test_work_orders.py:404`), and is documented as "what Dev 2's Cora
throttle reads". The throttle never calls it.

Ask: call `queued_depth(older_than=...)` alongside the count check, and
auto-pause if either trips.

## Question

`reclaim_stale_executing` in `src/services/work_orders/__init__.py` recovers
work orders abandoned by dead workers by moving EXECUTING→APPROVED after a
**30-minute wall clock**. There is **no lease and no heartbeat** — the code
flags this limitation about itself.

So a dispatch that merely runs *slowly* — not dead, just slow — is reclaimed
while still live, picked up by another worker, and **executed a second time**.

Today the only registered dispatcher is `noop`, so this is harmless. Ticket 06
put the sequencer in this framework, and ticket 05 will register a real email
dispatcher. At that point the failure mode becomes: **the prospect receives
the same cold email twice, and it cannot be un-sent.**

That is worse than it sounds. A duplicate cold email is a spam signal, it
feeds the bounce/complaint rates the deliverability sentinel watches (>3% /
>0.08% in a rolling 48h), and Link 2 of the acceptance contract is judged on
bounce under 3%. A double-send bug can quarantine a warmed domain.

Decide:

1. **Lease with heartbeat**, or **send-side idempotency**? A lease fixes the
   framework for every future dispatcher; send-side idempotency fixes only
   email but is unavoidable anyway as defence in depth. Probably both — a
   lease to stop the reclaim, and a pre-send check against the dispatch record
   so that even a reclaimed duplicate cannot transmit.
2. **What is the right timeout** for an SMTP send with an attachment? 30
   minutes was chosen for a `noop`. A real send is seconds; a hung connection
   could be minutes. The window between "clearly hung" and "still working"
   needs a number, and it should be per-dispatcher rather than global if
   different action classes have different profiles.
3. **Where does the pre-send idempotency check read from?** The sequence state
   model (ticket 07) will hold per-touch dispatch records. If a touch already
   has a recorded `Message-ID`, it has already been sent — that is the natural
   guard, and it costs one query.
4. **Ownership.** The code is Dev 2's; the danger is Task 3.1's; ticket 06
   made operating the framework Dev 3's. Coordinate rather than fixing it
   silently in someone else's module — but it must not ship unfixed.

## Acceptance

A dispatch that exceeds the reclaim timeout while still running must not
result in a second transmission. Prove it with a test that holds a dispatcher
open past the timeout and asserts exactly one send.

## Why it matters

Every other correctness bug on this map produces a missing record or a blocked
send. This one produces an **unwanted real-world side effect** at the
prospect, and there is no compensating action.
