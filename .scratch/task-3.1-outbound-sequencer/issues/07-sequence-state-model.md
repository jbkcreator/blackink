# Design the sequence state model

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: — (06 closed; 05's open part is noted below, not blocking)

## Resolution (2026-09-03) — cross-checked against the Source of Truth

### A separate `sequence_runs` table, one row per enrollment

Not derived from `agent_work_orders`. Ticket 06 made work orders the per-touch
execution vehicle, and everything *could* be derived from them — except the
active-sequence lock, which needs exactly one row per run to be unique on.
Work orders are one row per *touch*, so there is nothing to constrain. Without
the table you are back to a check, and back to the race.

`sequence_runs` holds the run; work orders reference it and carry execution.

### The lock is a partial unique index, not a check

```
UNIQUE (contact_id) WHERE status = 'ACTIVE'
```

Same pattern `county_allocations` already uses for one-active-row-per-county.

**Endorsed directly by the Source of Truth**, which criticises the blueprint's
own code for the opposite choice:

> "`idx_opportunity_dedupe` is an ordinary **non-unique** index, not an
> exactly-once financial constraint. **Preserve opportunity-level billing
> idempotency explicitly.**" — §5.2
>
> "An **entity lease prevents contradictory concurrent work; it does not
> itself prove a send or charge is unique.**" — §2.7

**The index is deliberately cross-client**, and that is the point: indexes do
not respect RLS, so this enforces "one active sequence per contact" globally —
the guarantee an RLS-scoped query cannot give. Ticket 01's cross-client
fatigue guard needs exactly this.

**Documented exception:** §2.1 says "isolate database records… by
`client_id`". This index is deliberately not isolated. That is consistent —
isolation governs *visibility and access*, not global safety constraints, and
§2.1 itself requires suppression that works "across clients and sending
domains at dispatch time". Record it as a deliberate exception so nobody
"fixes" it into a per-client index later.

**Consequence to handle:** client B's enrollment fails with an opaque
constraint violation against a row they cannot see. Correct security, poor
diagnostics — which is precisely why ticket 01's `may_enroll()` helper earns
its place. The helper returns a clean sanitized verdict in the normal case;
the index is the backstop that holds under race. Complementary, not redundant.

### All five touches enqueued upfront, with explicit cancellation

Enqueue at enrollment with `due_at` offsets; `due_batch()` (ticket 06) fires
them. Reject the alternative — enqueueing each touch when the previous sends —
because its failure mode is **invisible**: a failed enqueue after Touch 1
silently ends the sequence with nothing anywhere recording that it should have
continued. Absence does not alert. "Cancel a scheduled thing" is a state
transition you can see and audit.

Ground truth agrees on all three counts:

- §2.4 already uses the idiom: "**Every completion event cancels the relevant
  chase.**"
- §1.13 / Source A: "Immutable raw events plus versioned corrections… append-
  only history is right; permanent factual errors are not." So a cancelled
  touch is **transitioned, never deleted**.
- §2.1 requires preserving "**outbound touch history**" — the per-touch record
  is mandated, not incidental.

Cancellation triggers: opt-out, prospect reply, eligibility drift, halt. Note
§2.1's suppression must apply "at dispatch time, **including updates during a
running campaign**" — so the per-touch gate at execution (ticket 01) is the
real safety net, and cancellation is the tidy path, not the guarantee.

Cadence changes mid-flight require rewriting pending rows' `due_at`. Accepted
cost.

### `Message-ID` gets its own column on the run row

`execution_receipt` JSONB on the Touch 1 work order is the natural home, but
Touch 3 must read it four days later — meaning a query into a sibling work
order's JSONB by touch step. Touch 3's threading is a **DoD line**; make it a
direct read. Depends on ticket 05 confirming the sender returns a
`Message-ID` at all.

### Cooling: 30 days lives on the run, 14 days stays on the contact

The completed `sequence_runs` row carries its own `completed_at`; re-entry
reads that. `contacts.last_outbound_touch_at` stays purely the platform
fatigue signal. **One column, one meaning** — the absence of which caused the
original conflict in ticket 01.

### Required by §5.2's critique of the blueprint's own DDL

> "Several example tables omit explicit **tenant keys, correction versions,
> immutable-history protections, or foreign-key relationships** needed by the
> stated invariants. Printed DDL is not a complete application schema."

So `sequence_runs` must carry: `client_id` (registered in
`config/tenant_policies.py` **and** pushed through
`migrations/apply_rls_policies.py` — no automatic net), a real FK to
`contacts`, and a correction-version story consistent with ticket 03's
versioned-corrections design.

### Three scheduling states, not two

"Not yet due", "due and awaiting approval", "approved and awaiting dispatch" —
ticket 21's every-send review makes the middle state the common case, not the
exception.

### Other carried requirements

- **Carry our own dispatch timestamp in the event payload** (ticket 02) —
  never rely on `events.created_at`, which is set at insert and is wrong for
  buffered events.
- **Natural idempotency key** (ticket 06): contact + touch step + sequence run.
  The default helper is hour-bucketed and would re-enqueue hourly.
- **Card posting gates on `due_at`** — otherwise a Day 10 touch posts its
  approval card on Day 0.
- **Reply detection**: the stop condition "a prospect reply has been recorded"
  reads from 3.1.3's bridge. What exactly it reads is still open — blocked
  behind ticket 20's ingestion design.
- **Halt at enqueue as well as execution?** Carried from ticket 06, still
  open. Today a halted client accumulates QUEUED rows and posts cards; §1.4
  says a pause "must never re-arm".

## Question

No table anywhere tracks a contact's progress through the five touches.
Design it.

It has to answer, for any contact: which touches have been dispatched and
when, which is next and when it is due, whether the sequence is halted and
why, and what Touch 1's `Message-ID` was.

Specifics to settle:

- **Grain.** One row per sequence run with per-touch columns, or one row per
  touch? Per-touch rows make "did Touch 3 dispatch" a query rather than a
  column read, and give a natural idempotency key — but need a parent to
  hold sequence-level state.
- **Message-ID persistence** (§5.2). Touch 1's `Message-ID` must be readable
  when Touch 3 is built, days later. Which table, which column, and what if
  the sender never returns one (see ticket 05)?
- **The 30-day cooling timestamp** (§6.2). Written after Touch 5, and must
  "prevent further cold sequence activity during the cooling period". Is
  this a new column, or does it reuse `contacts.last_outbound_touch_at`?
  Note ticket 01 is untangling an existing 14-day cooldown on that same
  column — do not add a third overlapping concept without saying how the
  three relate.
- **Halt state.** §3.2 requires halting *permanently* on opt-out, reply, or
  ineligibility. Is halt a status enum on the sequence row, or derived each
  time from contact state? Derived is simpler but loses *why* it halted;
  §17.3's `Mark Opt-Out` must halt the sequence, and 3.1.2's DoD wants a
  logged skip — both want a reason.
- **Reply detection.** "A prospect reply has been recorded" is a stop
  condition (§3.3), but replies arrive through 3.1.3's bridge. What does the
  sequencer actually read to know a reply happened?
- **Tenant isolation.** Any new table is tenant-bearing and MUST be
  registered in `config/tenant_policies.py` and pushed through
  `migrations/apply_rls_policies.py`, or it gets neither RLS nor
  leakage-test coverage. Confirm the `client_id` column and policy shape.

Deliver the schema as a proposed `migrations/apply_<name>.py` shape plus the
`TENANT_POLICIES` entry — as a design, not applied.

## Update from ticket 01 (closed 2026-09-03)

Three requirements land here directly:

1. **The active-sequence lock.** Enrollment must be refusable on the grounds
   that the contact is already in a live sequence — checked on **state, not
   time**, because a stalled sequence (awaiting approval) lets a purely
   time-based guard go stale and admit a second, concurrent sequence to the
   same contact. The state model must make "is this contact currently
   enrolled anywhere" a cheap, race-free query. Note the lock is
   **cross-client**: `last_outbound_touch_at` is deliberately global, and
   county reallocation can hand a contact to a different client mid-flight,
   so the lock cannot be scoped by `client_id` — which puts it in tension
   with RLS. Resolve that explicitly.
2. **Three scheduling states, not two.** "Not yet due", "due and awaiting
   approval", and "approved, awaiting dispatch" are distinct (ticket 21's
   every-send review makes the middle one common, not exceptional).
3. **The 30-day cooling gates re-entry, the 14-day gates entry.** Two
   different concepts, so decide whether both read `last_outbound_touch_at`
   or whether the sequence row carries its own completion timestamp. Ticket
   01 recommends the latter — one column meaning two things is what caused
   the original conflict.

Also: **writing `last_outbound_touch_at` is what arms the fatigue guard.**
Nothing writes it today, so the guard is currently inert. The write and the
enrollment check must land together, or the guard silently does nothing.

### Tenancy split (decided 2026-09-03) and its three open refinements

The sequencer runs as the **client-scoped** role. A **narrow privileged
helper** does the cross-client `last_outbound_touch_at` and active-sequence
lookups under the system role and returns **only a verdict, never rows**.

Still to settle here:

1. **Sanitize the verdict before it is audited.** `compliance_gate_checks` is
   tenant-bearing. `_check_cooldown`'s current detail —
   `f"last touch {elapsed.days}d ago, cooldown not elapsed"` — would write
   another client's touch timing into this client's audit row. Decide the
   non-revealing reason string, while keeping "why didn't this contact get
   emailed" answerable to **us**. Possibly: sanitized detail in
   `compliance_gate_checks`, full detail in a system-scoped log.
2. **Make the lock a constraint, not a check.** A verdict returned from a
   separate system-role transaction is TOCTOU-racy: another client can enroll
   between the check and the write. Recommended: a **partial unique index** on
   `contact_id` where the sequence is active — the exact pattern
   `county_allocations` already uses for one-active-row-per-county — so the
   verdict becomes a friendly early-out and the database is the real
   guarantee. Alternative: a Postgres advisory lock. Pick one.
3. **Confirm enrollment is batch-side only.** `CLAUDE.md` restricts
   `blackink_system` to "internal batch jobs ONLY — never imported from
   `src/api/`". A background worker is fine; an API-triggered "start campaign"
   endpoint would violate it.

Name the helper so misuse is obviously wrong — `may_enroll(contact_id)`
returning a verdict, not a general cross-tenant contact reader. Once a
BYPASSRLS accessor exists in `src/services/`, the next person will reach
for it.
