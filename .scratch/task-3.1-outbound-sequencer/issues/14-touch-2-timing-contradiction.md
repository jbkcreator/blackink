# Resolve the Touch 2 timing contradiction

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03) — dissolved by `Week1_Tasks_Dev_Split_v2.md`

**The contradiction was an artefact of imprecise wording, and v2 fixes it:**

> "card appears within **60 seconds of Touch 1 dispatch approval**"
> — Subtask 3.1.2, Business Requirements
>
> "Touch 2 Slack card appears in `#dial-tasks` within 60 seconds of Touch 1
> **Approve action**" — Definition of Done

So the 60 seconds runs from the **human clicking Approve**, and Day 1–2
remains when the *call* is expected. Card creation and call timing were never
the same event. The reconciliation I proposed was right; v2 states it.

**Consequences:**

- **No sub-minute scheduler needed.** The card is created in response to an
  event (the Approve click), not discovered by a sweep. Ticket 06's day-grain
  sweep design stands — this was the one constraint that might have forced
  otherwise, and it doesn't.
- **Card must carry a due indication.** The call is Day 1–2 but the card
  appears on Day 0, so a setter working the queue needs to know not to call
  yet. v2 does not specify this; it follows from the two timings.
- **Calling hours are now specified**: green/red indicator against
  **8 AM–9 PM recipient local time** (v2 3.1.2). The Dev 3 reference gave only
  "local calling hours"; the DoD's 8 PM test is inside that window, so an 8 PM
  contact shows **green**, not red as the old reference claimed. Verify
  against the client — v2's own DoD still says "shows red for a test contact
  whose local time is 8 PM", which contradicts its own 8 AM–9 PM range.

⚠ **Carry forward:** that last point is a live contradiction *within v2*.
8 PM is inside 8 AM–9 PM, so the indicator should be green, but the DoD
asserts red. Either the window is wrong or the test is. → CLIENT-ASKS **D15**.

**Still open here:** whether the card re-evaluates its calling-hours indicator
when viewed. A static card posted Day 0 showing green is wrong by the time
someone reads it Day 2, and the indicator is a compliance display.

## Question

The source contradicts itself and the reference doc refuses to silently pick
a side (§11.2, §26.2):

- The **sequence table** (§1) places Touch 2 at **Day 1–2**.
- Subtask 3.1.2's **business requirement and DoD** say the Slack card must
  appear in `#dial-tasks` **within 60 seconds of Touch 1 dispatch**.

These are only contradictory if "the card appears" and "the call happens"
are the same event. The obvious reconciliation is that they are not: the
card is *created* immediately so it enters the setter's queue, and the call
is *expected* on Day 1–2. Confirm that with whoever owns the requirement
rather than assuming it — the DoD is a testable acceptance line and it says
60 seconds.

If that reconciliation holds, settle what follows:

- Does the card carry a **"call after" / due timestamp** so a setter working
  the queue does not call on Day 0, undercutting the sequence design?
- Should the card be **visually deferred** — posted immediately but marked
  not-yet-due — or posted immediately and simply ordered by due time?
- Does the 60-second requirement force an **event-driven** path? Ticket 06's
  scheduling options are mostly `while/sleep` sweeps; a 300-second sweep
  cannot meet a 60-second SLA. This is the one hard latency constraint in
  3.1.1/3.1.2 and it may dictate the mechanism.
- Is Touch 2's **compliance re-check** (§11.5) done at card creation, at
  call time, or both? The calling-hours indicator (§11.4) is inherently
  call-time — it shows whether it is valid to call *now* — which implies the
  card re-evaluates when viewed, not just when posted. Does a Slack card do
  that, or does it need refreshing?

That last point is substantive: a static card posted on Day 0 showing a
green calling-hours indicator is *wrong* by Day 2, and §11.4's DoD tests the
indicator's correctness.

## Why it matters

The 60-second figure, taken literally, constrains the scheduler design for
all of Task 3.1.
