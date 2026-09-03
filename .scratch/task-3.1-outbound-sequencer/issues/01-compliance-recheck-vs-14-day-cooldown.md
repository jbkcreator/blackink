# Reconcile the per-touch compliance re-check with the 14-day cooldown

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03, grilling)

**Split the gate into two stages. The cooldown moves to enrollment; the
per-touch re-check keeps everything else.**

### The two stages

| | Enrollment gate | Per-touch gate |
|---|---|---|
| When | Contact enters a sequence | Before every email touch |
| `email_verified` (opt-out, suppression, eligibility, `email_status`) | ✅ | ✅ |
| `dnc_clean` | ✅ | ✅ |
| `non_poach` | ✅ | ✅ |
| Prior prospect reply | ✅ | ✅ |
| **14-day contact-fatigue cooldown** | ✅ | ❌ **not applied** |
| **Active-sequence lock** | ✅ | n/a |

Implement as **two named entry points** — e.g. `evaluate_enrollment_gate()`
and `evaluate_touch_gate()` — both delegating to a shared check list, rather
than a `skip_cooldown=True` boolean. A bare boolean on a compliance call is a
footgun: it reads as innocuous at the call site and silently disables a safety
check. The names should make the stage obvious in review.

### Why the cooldown moves rather than disappears

It is **not** vestigial. `Contact`'s docstring (`src/core/models.py:225`) is
explicit and deliberate:

> "`last_outbound_touch_at` is deliberately global (not per-client) — the
> 14-day cooldown is a **platform-wide contact-fatigue guard**, not a
> per-client one."

Its real job is stopping Blackink, as a platform, from hammering one human
across multiple clients. That job matters more than it first appears:
`county_allocations` reassesses every 30 days and `companies.owning_client_id`
is a materialized reflection of it — so client A can sequence a firm, the
allocation can flip, and client B can enroll the same contact days later. The
enrollment-stage check catches exactly that.

What it must **not** do is block the campaign it is already inside. Three
touches in ten days is contact fatigue by the guard's own standard — the
blueprint's cadence and Dev 1's guard were designed by different people with
different intuitions, and neither knew about the other.

### Three parts, all confirmed

1. **Per-enrollment guard.** The 14-day check runs when a contact enters a
   sequence, not before each touch. After Touch 5, the 30-day cooling (§6.2)
   governs re-entry — so the two cooling concepts stop competing for one
   column: 14 days gates *entry*, 30 days gates *re-entry*.
2. **Active-sequence lock.** A time-based guard alone has a hole: if a
   sequence stalls (awaiting approval — now every send, see ticket 21) and
   15 days pass, another client's enrollment check sees a stale timestamp and
   passes, producing two concurrent sequences to one contact. So enrollment
   also requires that the contact is **not in an active sequence**, checked on
   state rather than time. Lands in ticket 07's state model.
3. **Cadence floor.** §2.3 makes the cadence "configurable". A configurable
   cadence with no floor lets a client configure their way past the fatigue
   policy. The floor is a platform-level minimum spacing that client
   configuration cannot go below. The floor's actual value is **not yet set** —
   see open items.

### The DNC branch (settled earlier in the same session)

- A **known DNC hit blocks** an email touch. Cross-channel courtesy beyond
  CAN-SPAM, which does not govern email by DNC.
- A **missing phone is not a hit** → PASS. There is no number for the registry
  to be silent about. This is a one-line change to `_check_dnc`'s first
  branch, currently `ABSTAIN, "no phone on file to check"`, and it removes the
  permanent-block trap for phoneless contacts.
- **DNC clearance moves to promotion time.** The Tracerfy sweep is monthly and
  the gate's live `DncProvider` is a stub that ABSTAINs, so a fresh prospect
  was unmailable for up to 31 days and "Touch 1, Day 0" was impossible.
  Scrubbing at promotion preserves the FTC 31-day safe harbor and puts the
  ~$0.02/phone cost at ingest. **Requires a change to Dev 1's
  `promotion_sweep` — negotiate before implementing.**
- Unchecked and stale both **block** (ABSTAIN). Both are transient under
  scrub-at-promotion, so they delay rather than kill. *Working answer — stated
  but not explicitly confirmed by the user; re-confirm if it becomes
  load-bearing.*

## Open items this created

- **The cadence floor's value is unset.** Ticket 12 or a new ticket should
  fix it. Note it must be compatible with Day 0/4/10, so it is at most 4 days
  — which is well under the 14-day fatigue standard, and someone should
  acknowledge that tension explicitly rather than let it pass silently.
- **`_check_cooldown` leaves the default check list.** That changes
  `evaluate_compliance_gate`'s behaviour for every existing caller. Audit
  callers before moving it, and keep the `compliance_gate_checks` audit trail
  recording which stage ran — "why didn't this contact get emailed" must stay
  answerable, and it now has two possible answers.
- **Nothing writes `last_outbound_touch_at` today.** The guard is inert until
  the sequencer starts writing it. Writing it is what arms the guard — so the
  enrollment check and the write must land together, or the guard silently
  does nothing.
- **`email_status` is still the harder blocker.** Every promoted contact is
  `UNVERIFIED`, nothing sets `VERIFIED`, and `evaluate_compliance_gate`'s
  `email_provider` parameter is accepted and never used. Both gates fail at
  the first check regardless of any of the above. Tracked in ticket 11.

## Question

§3.3 requires reading the contact's compliance state before **every** email
touch, and halting unless it is `EMAIL_COLD_ELIGIBLE`. The obvious
implementation is to call `evaluate_compliance_gate(...)`.

But `_check_cooldown` in `src/services/compliance_gate.py` FAILs unless
`last_outbound_touch_at` is at least 14 days old. Touch 3 is Day 4 and
Touch 5 is Day 10. So calling the existing gate before every touch — exactly
what the spec demands — makes Touch 3 and Touch 5 **always** fail.

One of these has to give. Which?

- Does the sequencer bypass `evaluate_compliance_gate` and implement a
  narrower intra-sequence check (opt-out, suppression, eligibility, prior
  reply) that deliberately omits the cooldown?
- Or does the gate gain an intra-sequence mode — a caller-supplied flag or a
  distinct entry point — that suppresses the cooldown check for contacts
  already inside a live sequence?
- Or is the 14-day cooldown itself wrong, and it should measure time since
  *sequence end* rather than time since last touch?

The answer must also settle: what is the cooldown actually protecting
against, and does the 30-day post-Touch-5 cooling period (§6.2) subsume it?
Two overlapping cooldown concepts on the same column is a smell.

Whatever is decided, the narrower check still has to catch everything §3.3
lists: `is_opted_out`, eligibility drift away from `EMAIL_COLD_ELIGIBLE`,
a recorded prospect reply, and general gate failure.

**This is the first ticket.** Nothing about email dispatch can be built
while the gate structurally forbids the cadence.

## Why it matters

Getting this wrong in the permissive direction means sending to people who
opted out — a compliance breach, not a bug. Getting it wrong in the strict
direction means the sequence silently never progresses past Touch 1.
