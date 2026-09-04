# Engine configuration row and holdout settings

Label: `wayfinder:grilling`
Status: open
Assignee:
Blocked by: —

## Question

The map's design has `sequence_runs` (ticket 07) and `agent_work_orders`
(ticket 06) and **no engine configuration row**. The blueprint requires one:

> "Engines are **configuration row + execution adapter**, carrying routing,
> templates, sequence, **county scope**, tier, and **holdout settings**."
> — Implementation Blueprint §2, core invariants

Ticket 06 established the cadence is "configurable" (§2.3) but never said
where configuration lives. Per the invariant it belongs in an engine row, not
scattered across constants, per-client columns, and prompt files.

Settle:

1. **What is an "engine"?** One row per client per campaign type — cold
   outbound, dead-book reactivation, Respond — each with its own cadence,
   template set, and county scope? Or one row per client? The blueprint lists
   several named recipes (Win-Back, Speed-to-Lead), which implies per-recipe.
2. **What does the row carry?** The invariant names six things: routing,
   templates, sequence, county scope, tier, holdout settings. Map each onto a
   column, and decide which are JSONB versus first-class.
   - *sequence* is the day offsets — Day 0/1–2/4/7/10 as configuration, with
     ticket 01's cadence floor as a platform minimum the row cannot go below.
   - *county scope* interacts with `county_allocations`, which already governs
     who may work a county. Is engine county scope a further narrowing, or a
     duplicate of allocation? **Do not create a second source of truth for
     who-may-contact-whom.**
   - *tier* is undefined anywhere. Ask.
3. **`sequence_runs` should FK to it.** A run then records which engine
   configuration produced it, which is what makes a mid-flight cadence change
   auditable rather than mysterious.

## Holdouts — the part with no design at all

**Holdout settings appear nowhere in the map.** A holdout is a control group
deliberately excluded from sends so campaign lift can be measured against a
baseline.

- It changes **enrollment**, not reporting: a held-out contact must not be
  enrolled, and must not be counted as a failure or a skip.
- Which means it interacts with ticket 01's enrollment gate and ticket 07's
  active-sequence lock — a holdout is a *fourth* reason enrollment can decline,
  alongside fatigue, active sequence, and compliance.
- And with ticket 12's champion/challenger experiment registry (§2.7), since
  holdout and variant assignment are both experiment concerns and should not
  be invented twice.

Settle: is holdout assignment per contact, per company, or per county? Is it
sticky across sequence runs — a contact held out once stays held out — or
re-rolled each enrollment? Sticky is almost certainly right, since re-rolling
destroys the comparison, but it needs a stored assignment rather than a
runtime dice roll.

Also: does a holdout contact still get **scored** (Dev 2) and still appear in
the digest denominator? Otherwise the lift measurement has no baseline to
compare against.

## Why it matters

Blocks nothing today, so it is easy to defer — and expensive to retrofit.
Adding an engine FK to `sequence_runs` after runs exist means backfilling
rows against a configuration nobody recorded. Land it before ticket 07's
migration.
