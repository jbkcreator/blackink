# Choose the counting substrate for the 24h per-mailbox send cap

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03)

### Substrate: a dedicated `mailbox_sends` table

- **Events ledger — eliminated by ticket 02.** Dev 4's logger buffers on DB
  failure in a **process-local** list lost on restart, and buffered events are
  re-timestamped at flush. Both errors undercount, and both push
  **permissive** — over the cap, burning a warmed domain. A safety control
  cannot be built on a best-effort log.
- **Redis counter — rejected.** Fast, but volatile: a flush silently resets
  the cap and **the 60→50 DoD test would still pass**. A safety control whose
  failure mode is invisible to its own test is the worst available option.
- **Dedicated table — chosen.** Durable, auditable, joins cleanly, survives
  restarts. One write per send is negligible next to an SMTP round trip.

Natural fit: the per-touch dispatch record from ticket 07/22 already carries
`UNIQUE (sequence_run_id, touch_step)` and is written *before* the send. Count
against that rather than a second table, if the columns line up — one write,
not two, and the cap then counts *claimed* sends, which is the conservative
direction.

### Window: rolling 24h

**Only reading that satisfies both sources.** Blueprint p16 says "a **daily
ceiling** of 30–50 cold emails **per day**"; the Task 3.1 DoD says "no more
than **50 emails in a 24-hour window**". Calendar-day satisfies the first and
fails the second. Rolling satisfies both.

Rolling is also the honest deliverability control — calendar-day permits 50 at
23:00 and 50 at 01:00, which is 100 sends in two hours from one mailbox and
exactly the burst pattern the cap exists to prevent.

### Enforcement point: inside `get_active_mailbox_for_client`

A capped mailbox is never handed out, so no caller can forget the check. This
changes the shared function's contract and its `NoMailboxAvailable` semantics
— coordinate with ticket 09, which is already modifying the same query to join
`sending_domains`. **Do both in one change.**

### All mailboxes capped: defer, do not fail

The current docstring says the caller routes `NoMailboxAvailable` to DLQ.
**A DLQ'd touch is a lost touch**, which is wrong for a condition that clears
in hours.

§2.8's "retry transient failures with backoff, at most three attempts before
DLQ/incident" governs **failures**. A capped mailbox is not a failure — it is
a scheduled constraint working correctly. So DLQ is the wrong disposition.

Defer instead: push the touch's `due_at` forward and let ticket 06's
`due_batch()` retry it. Two guards needed:

1. **Distinguish the causes.** "All capped" (defer) and "no mailboxes
   provisioned" / "all quarantined" (a real incident) currently raise the
   same `NoMailboxAvailable`. Deferring forever on a provisioning failure
   would hide it. Split the exception or carry a reason.
2. **Bound the deferral.** A touch that defers indefinitely silently drops out
   of its sequence. After N deferrals, alert `#blackink-qa`.

### The cap value: 50, configurable, per client

The range is 30–50 and the DoD asserts 50. Make it a per-client config with a
platform ceiling of 50 — warmup ramp is a real reason to start lower.
`clients.daily_send_ceiling` already exists and is unused; **confirm whether
it is this concept** before adding a second column. Note Relay's hard stop
(blueprint p32): it "cannot bypass daily mailbox limits" — so the ceiling must
not be agent-adjustable.

### Acceptance

Beyond the DoD's 60→50 test, the September bar is **Link 2** (§1.12):
"Campaign to **100 verified addresses** with **bounce under 3%**; deliberately
noncompliant draft hard-blocked; **mid-campaign suppression prevents any later
send**." Note the 100-address test needs ≥2 mailboxes under a 50 cap — which
makes rotation, not just capping, part of what it proves.

## Question

§7.2 requires 30–50 cold emails per mailbox per day, and the DoD demands a
hard proof: a **60-recipient batch must show no single mailbox exceeding 50
sends in a 24-hour window**.

`get_active_mailbox_for_client` currently does LRU rotation with
`FOR UPDATE SKIP LOCKED` and no send counting whatsoever. There is no
counter table, no `sends_today`. `clients.daily_send_ceiling` exists and is
unused — decide whether it is the same concept.

Options, each with a different failure mode under the 60→50 test:

- **A dedicated `mailbox_sends` table** — durable, auditable, joins cleanly
  to the cap query, but a write per send.
- **`COUNT(*)` over the events ledger** filtered on
  `outbound_touch_dispatched` + `mailbox_id` + a 24h window — no new table,
  single source of truth, but couples pacing to the event logger's
  availability, and ticket 02 may give that logger a *buffer* that delays
  writes. A buffered event is an uncounted send.
- **A Redis counter** via `tenant_redis` — fast, but volatile; a Redis flush
  silently resets the cap, and the DoD test would still pass.

Settle also:

- **Rolling 24h or calendar day?** "50 in a 24-hour window" reads rolling;
  "per day" reads calendar. These differ materially at a sprint boundary.
- **Where is the cap enforced** — inside `get_active_mailbox_for_client`
  (so a capped mailbox is never handed out), or by the caller after
  selection? Inside is safer; it changes the function's contract and its
  `NoMailboxAvailable` semantics.
- **What happens when every mailbox in the cluster is capped?** Defer the
  touch to tomorrow, or fail it? Deferral interacts with ticket 06's
  scheduling and with the Day 0/4/10 cadence.
- Is the cap **30 or 50**? §7.2 says the range is 30–50 and the test asserts
  50. Is the operative number per-mailbox configurable, per-client, or fixed?

## Update from the Source of Truth (2026-09-03)

The 30–50/mailbox/day baseline and the 3% bounce / 0.08% complaint rolling-48h
quarantine are **confirmed** (§2.2).

But the acceptance bar is now higher than the 60-recipient test. Nine-link
contract, **Link 2 — "Reach them"** (§1.12):

> "Campaign to **100 verified addresses** with **bounce under 3%**;
> deliberately **noncompliant draft hard-blocked**; **mid-campaign suppression
> prevents any later send**."

Three separate assertions, only one of which is about pacing:

- 100 verified addresses — so `email_status = VERIFIED` must actually be
  reachable (see ticket 11); today nothing sets it.
- bounce under 3% — a live deliverability outcome, not a unit test.
- mid-campaign suppression prevents any **later** send — the per-touch
  re-check from ticket 01, tested adversarially.

Also §2.2: "Maintain global suppression across every domain, **even if
inventory expands to 30 domains** as contemplated in comments" — so the cap
and suppression design should not assume the current domain count. See
`SENDING-DOMAINS.md` for the supplied inventory and its shortfall.

## Why it matters

This is a named, testable DoD line with an explicit adversarial test. It is
also a deliverability control — exceeding it burns warmed domains.
