# Touch 2 dial-task card — trigger, contents, and calling-hours freshness

Label: `wayfinder:grilling`
Status: closed
Assignee:
Blocked by: —

## Resolution (2026-09-03) — decided by user directive "go with your leans"

The four forks, settled on the recommended reading:

1. **Trigger: event-driven, not sweep.** Post the dial card the instant Touch 1
   flips to APPROVED — fire it from `_finalize_terminal_decision` in
   `listeners.py` (where the terminal decision is recorded), gated on
   `decision == "APPROVED"` and the order's `action_class` being the Touch 1
   email touch. The v2 DoD's "within 60 seconds of Approve" cannot be met by
   the 2–5 min day-grain sweep, so this is the one event-driven path in Dev 3.
   The `DIAL_TASK` work order's own Day-1 `due_at` is **not** what posts the
   card — it's only a "call after" hint rendered *on* the card. (So the
   `DIAL_TASK` order stays QUEUED as a record; the card is a side-effect of
   Touch 1's approval, not of the DIAL_TASK order becoming due.)

2. **Informational card + one "Mark Called" → DONE button.** No Approve/Reject
   gate — a phone task sends nothing, so there is nothing to gate. One button
   closes the `DIAL_TASK` work order to DONE for queue hygiene. Capturing a
   richer call *outcome* (connected / voicemail / no-answer) is deferred — see
   new client ask C9; September ships the plain DONE.

3. **Card contents: drop the two dead fields; degrade the Dev-2 fields.**
   Remove ghost-shopper latency (prohibited) and Sendspark watch % (held) —
   not defaulted, removed. Owner Visibility Score / county rank / three-lowest
   come from Dev 2 (ticket 19 fetch interface); until that ships, render them
   as a single "score pending" line and **still post the card** — the dial
   brief (name, company, door count, phone, local time) is useful without them.
   A permanently-dead field trains setters to ignore the card; a "pending" line
   that later fills does not.

4. **Calling-hours indicator: compute at post time, stamp it, default
   conservative.** Slack cards are static; building a re-render-on-view
   mechanism is out of proportion for September. Compute the indicator when the
   card is posted and print "calling window computed HH:MM ET — confirm local
   time before dialing" so the setter knows it's a snapshot, not live. On the
   **D15 contradiction** (window 8 AM–9 PM vs DoD asserting red at 8 PM):
   default to the **conservative** reading — treat 8 PM as already red
   (effective window 8 AM–8 PM) — because over-restricting calls is safe and
   under-restricting risks a TCPA violation. Flag D15 for the client to
   confirm the true cutoff; the indicator threshold is a one-line constant.

5. **Timezone: hardcode ET for September.** All ten launch counties are
   Eastern, so recipient-local == Blackink ET. Reuse the existing
   `America/New_York` constant pattern from `listeners.py` (`_SNOOZE_TIMEZONE`,
   already ponytail-noted). Defer the real `contacts.timezone` column to
   national rollout (D14).

**Nothing left to decide before building Touch 2** — dispatcher registration
for the card post + a `#dial-tasks` formatter, wired off the Touch 1 approval.

## Question

3.1.1 settled everything this ticket waited on: the work-order seat
(`agent_work_orders`, ticket 06), the day-grain sweep, the sequence state
model (ticket 07), and the per-touch compliance gate. `enroll_contact`
**already enqueues Touch 2 as a `DIAL_TASK` work order** with `due_at = Day 1`
— but nothing dispatches it. There is no `DIAL_TASK` entry in the
`DISPATCHERS` table, and no card formatter for `#dial-tasks`. This ticket
decides the contract for that card. Four sub-questions:

### 1. Trigger — approval gate, or automatic on Touch-1 approval?

Touch 1 (email) goes through the Approve gate because it *sends*. A phone task
sends nothing — it hands a human a brief. v2 says the card appears "within 60
seconds of Touch 1 **Approve action**" (ticket 14, dissolved). So the trigger
is Touch 1's approval, not an independent gate.

- Does `DIAL_TASK` post **automatically** (no Approve/Reject buttons — just an
  informational brief the setter reads before dialing), or does it carry a
  one-tap **"Mark Called" → DONE** button to close the work order?
- If automatic-on-Touch-1-approval: what fires it? An event in
  `_finalize_terminal_decision` when Touch 1 flips to APPROVED, or the
  day-grain sweep picking up the `DIAL_TASK` order at its own `due_at`? The
  60-second SLA (v2 DoD) rules out a 5-minute sweep — it likely forces an
  **event-driven** post at Touch-1 approval time, decoupled from the Day-1
  `due_at`.

### 2. Card contents — what replaces the two dead fields?

The §11.3 field list lost two fields to the Source of Truth: **ghost-shopper
latency** (prohibited, §1.8) and **Sendspark watch %** (video held, §1.5).
Both must be **removed, not defaulted** — a field permanently reading
"unavailable" trains setters to ignore the card. v2's own DoD names the
surviving fields: prospect name, company, door count, direct phone, local
timezone + current local time, Owner Visibility Score, county rank, three
lowest-scoring categories.

- The Owner Visibility Score, county rank, and three-lowest come from **Dev 2**
  (Task 2.1). Same fetch-interface dependency as Touch 1 (ticket 19). Until
  Dev 2 ships, does the card render those as "pending", or does Touch 2 not
  post at all? (Prefer: post with a "score pending" line — the call brief is
  still useful without it, unlike a permanently-dead field.)

### 3. Calling-hours indicator freshness

The green/red 8 AM–9 PM indicator (v2 3.1.2) is inherently **call-time** — it
shows whether it is valid to call *now*. But the card is posted Day 0 and the
call is Day 1–2. A static card posted green on Day 0 is **wrong** by the time
someone reads it Day 2, and the DoD tests the indicator's correctness.

- Does the card **re-render** its indicator when viewed (Slack cards are
  static — this needs a refresh mechanism), or is the indicator computed at
  post time with a visible "computed at HH:MM" caveat?
- **Live contradiction in v2 (D15):** the window is 8 AM–9 PM but the DoD
  asserts red at 8 PM. 8 PM is inside the window → should be green. Client
  must rule which is authoritative before the indicator is testable.

### 4. Timezone source

`contacts` has **no timezone column** (D14). Touch 2's card needs "local
timezone" and "current local time".

- **September simplification:** all ten launch counties are Eastern, so
  recipient-local == Blackink ET. Hardcode ET for September (the codebase
  already hardcodes `America/New_York` in `listeners.py` `_SNOOZE_TIMEZONE`
  with a ponytail note). Defer the real timezone column to national rollout.
  Confirm this is acceptable.

## Why it matters

Touch 2 is the smallest remaining Dev 3 build — all scaffolding exists, it
needs only a dispatcher + a card formatter. But the four questions above are
genuine forks (event-driven vs sweep, buttoned vs informational, indicator
freshness) that change the code shape. Settle them before building so the
dispatcher is written once.
