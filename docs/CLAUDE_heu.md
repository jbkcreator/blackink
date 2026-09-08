# Blackink — Project Context for Claude

**Updated 2026-09-03** against `Week1_Tasks_Dev_Split_v2.md` and
`Implementation_Blueprint_v2.md` (both v2 — Active Scope, Source of Truth
applied). Anything contradicting those two is stale.

## Client

Josh Kantor (Blackink / HEU AI LLC). Lead dev: Hari Krishnan (heu.ai).

## What it is

Multi-tenant B2B outbound platform for **residential property-management
firms**. Value proposition: more managed doors, fewer lost doors, more revenue
from the existing portfolio. The paying customer is the PM firm; the property
owner is the prospect served *for* that firm.

**Never a tenant-response service.**

## Geography — county, not metro

**The county is the unit everywhere**: data, ranking, contract, seat. There is
no metro layer. National coverage means many counties, not a different model.

Launch counties: **Hillsborough and Pinellas** first, then Orange, Duval,
Polk, Pasco, Lee, Brevard, Volusia, Seminole. Miami-Dade, Broward and Palm
Beach deliberately not first.

## ⛔ Cancelled — do not build

| Thing | Why |
|---|---|
| **Ghost shopper / pretext inquiries** | Explicitly prohibited. Replaced by the Owner Visibility Score |
| **Sendspark video, GIF thumbnails** | No account supplied; video deferred. September gets a trigger hook + disabled provider row only |
| **All SMS this year** | Outbound cancelled entirely. Inbound prospect-initiated capture only |
| **Twilio** | Replaced by **Telnyx** as carrier |
| **Calendly** | Google Calendar + Microsoft Graph; GoHighLevel fallback |
| **Metro targeting / `market_metro`** | County is the unit |
| **Rent Analysis Bot, RentCast/CoreLogic** | Deferred to Q1 |
| **AI voice** | Not this year, outbound or callback |
| **County seat SKUs** | Out of September; keep rows disabled |

## Owner Visibility Score — the replacement for the ghost shopper

100 points over **public observables only** (owner-addressed page 14, working
contact form/phone/email 10, mobile+HTTPS+<3s 6, review count vs county median
16, review recency 12, review response rate 18, average rating 8, after-hours
route 8, median response lag 4, broker licence tenure 4). Renamed from "Owner
Response Score".

Display rules: show **data coverage** alongside the score ("76/100, 94% data
coverage"); lead with the **three lowest-scoring observations** and named
comparisons, then score, then county rank. Publish **top 25 per county only,
never a bottom list**. Below the data floor: "insufficient data", no rank.

**Owned by Dev 2** (Task 2.1). Loss-report defaults: **8% management fee,
30-month average owner tenure, ~$100/door/month** — per-client configurable,
assumptions shown, labelled as estimates.

## Hard invariants — never violate

1. No LLM for compliance, billing, legal, or suppression. Deterministic gates
   only.
2. `client_id` on every DB row, Redis key, cache entry, audit record.
   Cross-tenant leakage = P0.
3. `events` is the system of record. **Write only through
   `src/services/events.py::log_event()`** — never raw `INSERT`.
4. Commercial terms are configuration rows, never hardcoded prices.
5. Cold outbound SMS hard-blocked at DB + application + CI/CD.
6. Slack buttons SHA-256 payload-bound; stale and expired clicks rejected.
7. **Every early-client send is human-reviewed** before dispatch. A template
   class earns autonomy only after 50 consecutively approved clean sends.
8. Raw events immutable, corrected by **versioned corrections** — never
   mutated, never deleted.
9. A Relay halt persists indefinitely. **A TTL or restart must never re-arm a
   paused campaign.**

## Week 1 dev split (Sept 1–11, gate Sept 11 2026)

| Dev | Owns |
|---|---|
| **1** | Data foundation: schema, ingestion, dedup, two-contact resolution, deterministic compliance gate, non-poach, DNC, cold-SMS block |
| **2** | **Owner Visibility Score engine**, county rank, score PDF report, Fee-Stack one-pager |
| **3** | **Outbound sequencer** (touches 1–5), interim reply bridge, booking engine, show-rate cascade |
| **4** | Demo sandbox, **event logging layer**, post-meeting modal, daily digest, Slack ops hub |

### Dev 3 sequence (all touches human-approved before send)

| Touch | Day | What |
|---|---|---|
| 1 | 0 | Email — Owner Visibility Score **PDF attached**, micro-ask "Reply YES to see where you rank in [County]". No GIF, no video link |
| 2 | 1–2 | Slack card in `#dial-tasks` within **60s of Touch 1 Approve**. Carries score, county rank, 3 lowest categories, local time, calling-hours indicator (8 AM–9 PM recipient local) |
| 3 | 4 | Email — Fee-Stack one-pager attached, threaded via `In-Reply-To` = Touch 1 `Message-ID` |
| 4 | 7 | Manual LinkedIn deep-link + clipboard note. No scraping, no browser automation, no LinkedIn API |
| 5 | 10 | Email — County Visibility Rank & Scarcity. Final cold touch, then 30-day cooling |

Pacing: **30–50 emails per mailbox per day**, rotated across the client's
6-mailbox cluster. Quarantine on bounce >3% or complaints >0.08% in a rolling
48h.

### Event names (exact — wrong names fail silently)

`outbound_touch_dispatched` · `email_opened` · `email_clicked` ·
`touch_skipped_compliance` · `linkedin_task_created` ·
`inbound_reply_received` · `opt_out_recorded`

`outbound_touch_dispatched` requires exactly six payload keys — `touch_step`,
`channel`, `recipient_email`, `template_version`, `sending_domain`,
`mailbox_id` — and `channel` must be the literal lowercase `"email"`.
Missing a key raises `MalformedEventError` and the event is **not written**.

## Sprint deadlines

- Week 0: Aug 31 – Sept 2 — platform remediation, Slack hub
- **Week 1: Sept 1–11 — marketing live** (gate: Sept 11 demo)
- Assisted pilot: **Sept 16–18**
- Week 3: Sept 21–25 — portal, cloner, preflight (Sept 25)
- Week 4: Sept 28–30 — verification, retention, expansion
- **Sept 30 final acceptance:** two-tenant zero-code repeatability

## Sept 11 shared Definition of Done

1. Live outbound dispatch from warmed inboxes **after Approve**; Reject and
   Snooze prevent dispatch
2. Owner Visibility Score report for a real Hillsborough/Pinellas firm; code
   review confirms **no ghost-shopper path exists**
3. Booking via Google Calendar; `meeting_booked` event; ICS attachment;
   **no SMS anywhere in the flow**
4. 30-minute pre-demo lead-in email with score PDF; no rent-bot number, no SMS
   instruction, no `blackink.io` reference
5. Slack hub Approve/Revise/Snooze; hash blocks altered payloads; **expired
   cards rejected**
6. CI/CD proves cold SMS hard-blocked

## Domains

`getblackink.com` is the protected brand domain. Blackink does **not** own
`blackink.io`. All others are cold-outreach domains — see
`.scratch/task-3.1-outbound-sequencer/SENDING-DOMAINS.md` for the inventory
and its shortfall against the 20-domain plan.

DNS/SPF/DKIM/DMARC and mailbox warmup are a **manual runbook, not code**. The
system reads `warmup_status` / `quarantine_state` and verifies at preflight.

## 9-agent workforce

Launch · Prospecting (Hunter) · Campaign (Cora + Relay) · Lead/Triage ·
Reactivation · Referral · Economics (Vera) · QA/Watchdog · Setter Copilot.
Shared Agent Core: tenant-scoped memory, typed work orders, risk/action
classes, payload hashes, receipts, leases, retries, idempotency.

An entity lease prevents contradictory concurrent work; **it does not prove a
send or charge is unique** — use an explicit unique constraint for that.

## Settings convention

Pydantic v2 + pydantic-settings. Never read `os.environ` directly outside
`config/settings.py`. Always `get_settings()`.
