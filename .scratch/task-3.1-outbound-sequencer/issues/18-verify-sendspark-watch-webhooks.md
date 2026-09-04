# TASK: verify Sendspark partial-watch webhook behaviour empirically

Label: `wayfinder:task`
Status: closed — MOOT
Assignee: —
Blocked by: 17 (also closed, out of scope)

## ⛔ Moot (2026-09-03)

There is no Sendspark account and no video flow in September (see ticket 17
and Source of Truth §1.5), so there are no watch webhooks to verify.

The three consumers this ticket existed to protect lose their data source
entirely, and each needs the field **removed**, not defaulted:

- Touch 2 phone card — "Sendspark video watch percentage, if available"
  (Dev 3 ref §11.3) → no source. Remove from the card spec.
- 3.1.3 reply card — "Sendspark video watch percentage" (§16) → remove.
- Daily digest — "video completion rate" (§25) → no source this year.

Removing beats defaulting: a card field permanently showing "unavailable"
teaches setters to ignore the card.

Reopen only if the video hold is reversed.

## Question

Ticket 10 found that `watchPercentage` arrives **only** by webhook — there
is no documented GET endpoint returning it, so there is no polling fallback
and no way to reconcile a miss. It also found the docs silent on latency,
and third-party sources claiming that in practice only "played" and "watched
100%" fire reliably, with partial watches possibly producing nothing.

Three consumers depend on this number: 3.1.2's phone card (§11.3), 3.1.3's
reply card (§16), and the daily digest's video completion rate (§25). All
three currently assume it is available.

Establish empirically, against the real workspace from ticket 17:

1. **Does a partial watch fire a webhook at all?** Watch a test video to
   roughly 40% and abandon it. Does anything arrive? If not, `watchPercentage`
   is effectively binary and the cards should say "watched / not watched"
   rather than a percentage.
2. **What is the latency** from watch to webhook delivery? 3.1.3 has a
   30-second surfacing SLA (§15.3) — though for replies, not watch events —
   and 3.1.2's card wants the number at card-build time, which may be
   minutes after the watch.
3. **What exactly fires, and when** — enumerate the observed events across
   a full watch, a partial watch, a replay, and a CTA click. Confirm
   `contactInfo.contactEmail` is populated in every case, since that is the
   only documented join key back to `contacts`.
4. **Are webhooks retried on failure?** This determines whether a receiver
   outage means permanent data loss, which in turn decides whether the cards
   need a "watch data unavailable" state distinct from "not watched".

## Answer should record

The observed event list, latency range, retry behaviour, and a clear ruling
on whether `watchPercentage` is a real percentage or effectively a boolean.
Downstream card design (fog: "3.1.2 Touch 2 card contents") depends on it.

**Design defensively regardless:** all three consumers must tolerate a null
watch percentage, since even reliable webhooks can be missed.
