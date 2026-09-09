<!-- wayfinder:map -->
# Dev 4 — Week 2 Sending, Speed-to-Lead & Pilot

## Destination

A locked decision-map + spec for Dev 4's Week 2 surface — Task 4.1 (tenant-isolated
sending), Task 4.2 (Speed-to-Lead: dual-path ingest, six-attempt cadence, pay-per-lead
routing), Task 4.3 (founding-client pilot Sept 16–18) — such that every open decision is
resolved and implementation sessions can build without re-deciding. Plan, don't build.

## Notes

- **Effort slug**: `dev4-week2`. Tracker: local-markdown (`.scratch/dev4-week2/`).
- **Driver**: Dev 4 (lesly.vj@heu.ai).
- **Sources**: `docs/Week2_Tasks_Dev_Split_v1.md` (Dev 4 = Tasks 4.1/4.2/4.3),
  `docs/Sept04_New_Items_Triage.md` (#13 six-attempt cadence, #14 pay-per-lead fold into 4.2).
- **Skills to consult**: `grilling` + `domain-modeling` per decision ticket; `research` for AFK facts.
- **Repo reality (scan 2026-09-07)**:
  - 4.1 sending is **mostly built** — `apply_sending_domains.py`, `mailbox_dispatcher.py`
    (LRU rotation, 24h cap), `deliverability_sentinel.py`, cross-tenant block all live.
    Two gaps: per-client ceiling not wired (hardcoded 50), reserve swap not cluster-scoped.
  - 4.2 Speed-to-Lead is **greenfield** — no `inbound_messages` table, no webhook, no
    `leads@` parser, no inbound cadence, `inbound_lead_received` undefined. Reusable:
    `booking_link.py`, non-poach gate (`compliance_gate.is_claimed_by_other_client`), `events.py`.
  - 4.3 pilot — tenant/RLS + demo-sandbox dashboard exist; **missing** tenant-clone/provision
    script and per-client Wins dashboard.
- **External givens (Q3 — NOT my tickets, assumed delivered by other devs)**:
  Dev 1 appointment/`meeting_booked` (EXISTS), Dev 1 settlement/Stripe, Dev 2 triage/intent
  classifier, Dev 3 Win-Back ingest + 3-touch + assessor/FRBO, Dev 3 owner enrichment.
- **⚠ PILOT RISK**: as of 2026-09-07 all external givens except Dev 1 booking are **absent
  from the repo**. Task 4.3 DoD (Sept 16) depends on settlement + triage + Win-Back + enrichment
  that are not yet built. Flagged, not owned.
- **Cross-dev / client questions** are parked as grilling tickets; Dev 4 relays client/team
  answers back — the agent never answers the human's side.

## Decisions so far

<!-- one line per closed ticket, gist + link -->

- [Research: Path B inbound email transport](issues/06-path-b-inbound-email-transport.md):
  **Mailgun Routes** (push, HMAC-signed, wildcard `*.getblackink.com` MX + one catch-all
  route serves all `leads@{sub}...`, no per-message charge). Verify signature before any
  `inbound_messages` write. Detail: `docs/research/path-b-inbound-transport.md`.
- [Wire per-client daily send ceiling](issues/01-per-client-daily-send-ceiling.md):
  IMPLEMENTED — picker resolves cap from `clients.daily_send_ceiling` (per mailbox); `0`/NULL
  → fallback 50; explicit arg wins. `mailbox_dispatcher.py` + tests (38 passed).
- [Cluster-scoped reserve domain swap](issues/02-cluster-scoped-reserve-swap.md):
  IMPLEMENTED (same-cluster + same-tenant promotion) but **⚠ REOPENED 2026-09-07** — conflicts
  with the client's supplied **3 global reserves** (SENDING-DOMAINS.md). Needs client/CEO call:
  global reserves (a) vs per-cluster reserves (b). Sentinel swap silently fails until settled.

- [Dual-path ingest orchestrator](issues/07-dual-path-ingest-orchestrator.md) +
  [30-min SLA auto-response](issues/08-sla-auto-response-business-hours.md): RESOLVED via
  grilling — full 4.2.1 design in [SPEC-4.2.1.md](SPEC-4.2.1.md). Webhook+Mailgun → one
  orchestrator, per-client secret auth, contact upsert, non-poach suppress, fixed-ET 30-min
  SLA, static template + booking_link, no LLM/SMS. [inbound_messages schema](issues/05-inbound-messages-schema.md)
  designed as a superset but **parked pending Dev 2** column relay before the migration lands.

## Not yet specified

- Per-portal parser specifics (APM / Manage My Property / Thumbtack field maps) — hangs on
  sample notification emails (Collect portal sample emails) and transport (Path B research).
- GHL sequence config-row schema for the cadence — hangs on the homegrown-vs-GHL choice.
- Concrete implementation hand-off (build slices) for every 4.2 component — produced only
  after its decision ticket closes; this map plans, it does not build.

## Out of scope

- #15 Live Inbound Demo Trigger, #16 Pixel + UTM Plumbing — Week 2 but unassigned to Dev 4 (Q2).
- Dead-Lead 9-Touch SMS Drip — client-blocked, no-SMS-this-year.
- All other-dev builds (settlement, triage, Win-Back, enrichment) — external givens, tracked
  in Notes, not charted as tickets.
