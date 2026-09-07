# Mark Opt-Out — halt mechanics on the settled sequence state model

Label: `wayfinder:grilling`
Status: closed
Assignee:
Blocked by: —

## Resolution (2026-09-03) — decided by user directive "go with your leans"

`Mark Opt-Out` resolves to one atomic service,
`halt_sequence_for_contact(contact_id)`, doing four things:

1. **Run transition: add a terminal `HALTED` state to `sequence_runs`.** Third
   terminal state alongside `ACTIVE` → `COMPLETED`, honoring §1.13
   transition-never-delete. **No `cooling_until` stamp** — an opt-out is
   permanent, not a 30-day cool-off; the durable block is `is_opted_out` on the
   contact, checked at the enrollment gate. `may_enroll` already refuses a
   contact with an ACTIVE run; it must **also** refuse on `is_opted_out = TRUE`
   (the authoritative signal — a HALTED run without the flag would let a
   different client re-enroll).

2. **Cancel the pending touches.** All 5 touches are enqueued upfront; on
   opt-out, transition the un-dispatched ones (QUEUED / SNOOZED) to a
   `CANCELLED` work-order state (add it if the state machine lacks it), matching
   §2.4 "every completion event cancels the relevant chase." **Layering:** the
   per-touch gate (`evaluate_touch_gate`, 3.1.1) already returns
   `COMPLIANCE_BLOCK` for an opted-out contact at send time, so cancellation is
   the belt (keep the setter's queue clean — don't post approval cards for an
   opted-out contact), and the gate is the suspenders (guarantees no send even
   if an order slips through). Both, deliberately.

3. **Cross-client scope: opt-out is global.** Set `is_opted_out = TRUE` on the
   contact (tenant-bearing, but the flag is a person's wish and morally
   cross-client) and let the enrollment gate honor it for **every** client
   thereafter. In practice the cross-client active-sequence lock (ticket 07's
   partial unique index) means at most **one** ACTIVE run exists, so there is
   one run to HALT — but the enduring guarantee is the gate refusing all future
   enrollment, not the single run transition.

4. **Wiring.** `halt_sequence_for_contact` does 1+2+3 in one transaction. The
   button lives on the 3.1.3 reply card (ticket 28's surface) and **must not
   expire** — ticket 15 already carved `Mark Opt-Out` out of card expiry. It is
   processed server-side with no human confirmation (§17.3) and logs
   `opt_out_recorded`.

**Buildable today — the only 3.1.3 piece not blocked on the alias scheme
(ticket 20).** The reply *card* that hosts the button is ticket 28 (blocked),
but the halt *service* and its `sequence_runs` / work-order transitions can be
built and unit-tested now, ahead of the ingestion path.

## Question

The map parked "opt-out halt wiring" as fog because *what "halt" means
mechanically depended on the sequence state model*. Ticket 07 settled that
model (`sequence_runs`, one row per enrollment, all 5 touches enqueued upfront
as `agent_work_orders`), so this graduates.

3.1.3's `Mark Opt-Out` button "deterministically sets `is_opted_out = TRUE`
and halts the sequence" (§17.3, v2 3.1.3), processed **server-side without
human intervention**, logging `opt_out_recorded`. Settle exactly what "halts
the sequence" does against the settled model:

### 1. The run transition

- `sequence_runs.status` today is `ACTIVE` → `COMPLETED` (via
  `complete_run_with_cooling`). Opt-out needs a **third terminal state** —
  `HALTED` (or `OPTED_OUT`)? Ticket 07's §1.13 "transition-never-delete" rule
  says add a state, never delete the run.
- Does halting stamp a `cooling_until`? An opt-out is permanent, not a 30-day
  cool-off — so **no cooling**; the block comes from `is_opted_out` at the
  enrollment gate, and `may_enroll` should also refuse a contact with a HALTED
  run (or rely purely on `is_opted_out`? — decide which is authoritative).

### 2. Cancelling the pending touches

All 5 touches are enqueued upfront as `agent_work_orders` with future
`due_at`. On opt-out, the un-dispatched ones (QUEUED/SNOOZED) must not fire.

- Do we **cancel** them (transition to a `CANCELLED` status), matching §2.4's
  "every completion event cancels the relevant chase" and §1.13's
  never-delete? `work_orders` has the state machine; confirm a CANCELLED path
  exists or is added.
- Belt-and-suspenders: the **per-touch gate already re-checks compliance** on
  every dispatch (3.1.1), so even an uncancelled order would return
  `COMPLIANCE_BLOCK` at send time. So cancellation is about **not posting the
  approval card** (setter shouldn't see a card for an opted-out contact), not
  about preventing the send — the gate already prevents the send. Confirm this
  layering: cancel to keep the queue clean, gate to guarantee safety.

### 3. Cross-client scope

`is_opted_out` is on the **contact**, which is tenant-bearing — but an opt-out
is a person's wish and is morally **cross-client** (if they opt out under
client A, client B must not email them either). Does opt-out halt only the
opting client's run, or every active run for that contact?

- The active-sequence lock is already cross-client (ticket 07's partial unique
  index). A contact can only have **one** ACTIVE run at a time, so in practice
  there is at most one run to halt — but confirm the intent: opt-out is
  global, and the enrollment gate must honor it for all clients thereafter.

### 4. Wiring point

`Mark Opt-Out` is a 3.1.3 reply-card button, but the handler is a work-order-
style Slack action. `Mark Opt-Out` must **not expire** (ticket 15 already
carved this out). Confirm the button lives on the reply card (ticket 28's
surface) and calls a new `halt_sequence_for_contact(contact_id)` service that
does 1+2+3 atomically.

## Why it matters

This is the one 3.1.3 piece **not** blocked on the client's alias scheme
(ticket 20) — the opt-out button's server-side effect is fully specifiable
today. It's also a compliance-critical path: a missed halt keeps emailing
someone who said stop. Worth settling precisely and independently of the
ingestion fog.
