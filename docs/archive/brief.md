# Project Blackink — Implementation Blueprint

**Client:** Josh Kantor, Blackink  
**Lead Developer:** Hari Krishnan (heu.ai)  
**Source:** Complete Implementation Blueprint (Full).pdf — received 2026-08-31

---

## 1. What Blackink Is

A **multi-tenant, deterministic B2B outbound sales + marketing platform** for property management companies across Florida metros (Tampa/St. Petersburg, Orlando, Miami-Dade).

It ingests 500–1,800 target property management companies, resolves their owner contacts, runs compliant cold outbound (email → phone → SMS waterfall), books discovery appointments, and auto-bills verified attended meetings via Stripe — with zero upfront fees.

---

## 2. Core Architectural Invariants (NEVER VIOLATE)

1. **No LLM for compliance, billing, legal, or suppression decisions.** These are deterministic hard-coded gates.
2. **Tenant isolation via `client_id`** on every DB row, Redis key, cache entry, vector embedding, and audit record. CI/CD runs adversarial cross-tenant leakage tests nightly.
3. **`events` table is the system of record.** Every learning loop, offer trigger, dispute eval, audit trail, and communication log reads from/writes to this ledger.
4. **Commercial terms = DB rows.** Every price, fee, tier, seat, escalator, credit, cap is a row in `entitlement_offers`. Never hardcode in application code.
5. **Payload-bound SHA-256 Slack approvals.** Every interactive Slack button is bound to a hash of the exact message payload + recipient ID + config state. Stale clicks are rejected.
6. **Engine Architecture = Row + Adapter.** Every revenue engine is a DB config row + a dedicated execution adapter. No logic in branches.

---

## 3. Tech Stack & Integrations

| Layer | Tool |
|---|---|
| Database | PostgreSQL (primary), Redis (leases, idempotency, cache) |
| Language | Python |
| Email sending | Instantly (30–50 cold emails/domain/day) |
| Video | Sendspark (dynamic merge-tag video landing pages) |
| SMS / 10DLC | Twilio |
| Billing | Stripe (Elements, ACH mandate, metered billing) |
| Calendar | Calendly / Google Calendar |
| Slack | Block Kit interactive hub (@Blackink router) |
| Entity resolution | Hunter (standalone async worker) |
| Contact enrichment | Anymail, Tracerfy |
| Dashboards | Looker Studio (PostgreSQL read-replicas) |
| Monitoring | UptimeRobot, Sentry |
| Public records | County deed recordings, MLS, tax assessor rolls, DBPR license registry |

---

## 4. Data Schema Overview

### Core Tables (migrations in `src/db/migrations/`)

| Migration | Table(s) |
|---|---|
| `001_core_spine.sql` | `events`, `companies`, `contacts`, `pm_profiles` |
| `002_triage_routing.sql` | `inbound_messages`, `knowledge_base_entries` |
| `003_settlement_ledger.sql` | `settlement_transactions` |
| `004_onboarding_states.sql` | `client_onboarding_states`, `client_growth_elections` |
| `005_preflight_validator.sql` | `client_preflight_checks` (10 GATE-XX booleans) |
| `006_verification_disputes.sql` | `appointment_outcomes`, `dispute_adjudications` |
| `007_expansion_registry.sql` | `entitlement_offers`, `client_active_entitlements` |
| `008_work_orders.sql` | `agent_work_orders` |

### Key Entity: Contact Model (Two-Contact Structure)
- **Contact A** (`OWNER_BROKER_MD`): Managing Broker / Owner / CEO — receives financial proof
- **Contact B** (`OFFICE_MANAGER_OPS`): Operations Manager / Lead PM — receives operational proof

### Ingestion Gate: `raw_prospect_pipeline` staging → validated into `companies` + `contacts`
A record transitions to `READY_FOR_CAMPAIGN` only when: email verified, DNC clean, not opted out, not suppressed, compliance eligible, has audit/video data OR personalized video ID, last outbound touch null or ≤14 days ago.

---

## 5. Compliance Architecture

```
[Prospect] → 1. Global Opt-Out Check → [FAIL: PERMANENTLY BLOCKED]
           → 2. Cross-Client Non-Poach Validation → [MATCH: SUPPRESSED]
           → 3. DNC Registry & Quiet Hours Check → [FAIL: CHANNEL SUPPRESSED]
           → 4. Warm-Channel Waterfall Routing
                 Cold tier: Email only (verified). Phone: task. Cold SMS: BANNED (CI fails build)
                 Engaged tier: Email + Transactional SMS (A2P approved)
```

- Cold SMS is **hard-blocked at DB, application, AND CI/CD** levels.
- Cross-client non-poach: read-only connections to active client PMS sync current owner rosters into a centralized suppression table.
- Metro Allocation: multiple PM firms in same metro → one client campaign at a time per owner, 30-day rotation via automated timer.

### A2P 10DLC Registration
- Legal Entity: HEU AI LLC, 971 US Highway 202N Ste N, Branchburg NJ 08876
- Campaign: "Mixed: Customer Care + Account Notification"
- Carrier: Telnyx / The Campaign Registry (TCR)

---

## 6. Sprint Timeline

### Week 0 (Aug 31 – Sept 2): Gate 1 — Platform Remediation & Compliance
- Core asset audit & reuse ledger
- 4 critical bug fixes (Vera silent-zero, relay halt/TTL, Cora queue throttle, Hunter entity resolution)
- Slack Agent Hub (@Blackink) live
- A2P 10DLC filing submitted
- Section 8 data pipeline interface (Akrash → `raw_prospect_pipeline` staging)

**Done when:** Slack cockpit live, defect-free health reporting, persistent halt verified, entity resolution pipeline operational, A2P filing submitted, signed data interface contract, documented reuse ledger.

### Week 1 (Sept 1–11): Sprint 1 — Marketing Live
- Live outbound campaigns (warmed Google Workspace / Outlook inboxes, dedicated domains)
- Ghost-Shopper audit factory + Sendspark dynamic video
- Inbound reply bridge → `#sales-replies`
- Calendly/GCal booking engine
- Pre-demo lead-in automation (30 min prior)
- Rent Analysis Bot (BOT-MIN): Twilio webhook → address parse → RentCast/CoreLogic → SMS reply <60s
- Looker pipeline digest to `#blackink-command`

**Milestone:** September 11 Marketing Live (live outbound, ghost-shopper audits, Sendspark video hooks, automated booking, demo sandbox).

### Week 2 (Sept 14–18): Sprint 2A — Settlement, Triage Agent & Founding Client Pilot
- Reply Triage Agent (1.4-Triage): 10 intent classes, ≥80% classification accuracy
- Stripe card auth + ACH mandate (zero deposit)
- 50/50 billing split: 50% at signature, 50% at Day 60
- 60-day automated clawback monitor
- Dynamic Evidence Packet PDF (4 sections)
- Tenant-isolated sending pools (20 domains / 40 mailboxes)
- Founding client pilot (Sept 16–18, hands-on engineering support)

**Milestone:** Sept 18 Founding Client Pilot Ready.

### Week 3 (Sept 21–25): Sprint 2B — 15-State Portal, Client Cloner & Demo Weapons
- 15-State Client Onboarding Portal (ADD-9-FULL): signed contract → live campaigns
- Path B Client Cloner Runbook (ADD-1-B): manual provisioning script
- 72-Hour Preflight Validator (PREFLIGHT): 10 GATE-XX checks, all must be green to arm campaigns
- Full Rent Analysis Bot (2A-BOT-FULL): multi-source (RentCast + CoreLogic), confidence scoring
- Churn Tripwire (2A-TRIPWIRE): nightly public records sweep (MLS, deed transfers, tax records)
- Owner & Portfolio Ingestion Engine (2A-FEED-MIN): LLC entity resolution
- Client Wins Dashboard (DASH-WINS) + Internal Control Center (OPS-CENTER)

**Milestone:** Sept 25 Sprint 2B Production Readiness.

### Week 4 (Sept 28–30): Sprint 3 — Verification, Retention & Production Complete
- 4-Rule Attended Appointment Verification Engine:
  - Rule 1: Assessor Parcel ID verified in polygon
  - Rule 2: Live attendance ≥12 minutes (proof_ref log)
  - Rule 3: Pre-qualification documented on outcome row
  - Rule 4: ICP alignment per Exhibit A
- Phase 3 Retention Suite: Retention Guard ($497/mo), Rent Gap Report ($197/mo), Owner Report Card ($197/mo)
- Expansion Engine: 17-row `entitlement_offers` registry, one-click in-app activation
- Production Security Baseline (SEC-BASE): cross-tenant leakage test suite, daily PostgreSQL backups + PITR, RBAC + encrypted secrets, wallet-capped paid growth
- **September 30 Acceptance Test:** Two-tenant zero-code repeatability (Golden Client → Tenant #2 via written runbook, ZERO code edits)

**Final Verdict: PRODUCTION COMPLETE — Full 600-Hour Platform Operational.**

---

## 7. The 9-Agent Workforce (AOS)

All agents run on a Shared Agent Core (chassis) with: tenant isolation, SHA-256 payload-bound approvals, distributed Redis leases, idempotency engine, persistent circuit breaker.

| # | Agent | Mission |
|---|---|---|
| 1 | **Launch Agent** | New signed contract → fully configured live tenant in 72hrs |
| 2 | **Prospecting Agent** (Hunter/Scout) | Entity resolution, multi-LLC aggregation, owner scoring |
| 3 | **Campaign Agent** (Cora + Relay) | Multi-touch outbound sequences, copy optimization, dispatch |
| 4 | **Lead Agent** (Reply Triage) | Classify & route 100% of inbound within 5 min |
| 5 | **Reactivation & Nurture** | Dead leads, no-shows, churn-risk → signed revenue |
| 6 | **Referral Agent** | B2B partner ecosystem (Realtors, vendors, lenders) |
| 7 | **Economics & Growth** (Vera) | Unit economics, cost ledger, governor bands, billing |
| 8 | **QA / Watchdog Agent** | Tenancy isolation, deliverability health, self-healing |
| 9 | **Setter Copilot** | 1-screen context cards, talk-tracks, post-call analysis |

### Autonomy Bands
- **Band 1 (Observe & Report):** Default. Zero autonomous external actions.
- **Band 2 (One-Tap Approval):** 50 consecutive clean approvals (≥95% rate) to unlock.
- **Band 3 (Bounded Auto-Execution):** 250+ clean executions, <2% dispute rate.
- **NEVER AUTONOMOUS:** Pricing, billing charges, refunds, legal replies, contract closing.

---

## 8. Slack Hub Channels

| Channel | Purpose |
|---|---|
| `#blackink-command` | Executive cockpit: quota queries, portfolio health, emergency global pause/resume |
| `#blackink-setter` | Human conversation cockpit: 1-screen context cards, hot leads, dial tasks |
| `#sales-replies` | Real-time inbound reply stream with intent tags and triage action buttons |
| `#dial-tasks` | Prioritized daily phone queue with direct lines and timezone calculations |
| `#client-{name}-growth` | Per-tenant growth workspace: quota pacing, active campaign stats |
| `#client-{name}-launch` | Per-tenant onboarding: 15-state checklist, preflight status, launch approvals |
| `#blackink-qa` | Health, deliverability alerts, leakage test reports |
| `#blackink-economics` | Per-tenant cost ledgers, contribution margins, wallet consumption |

---

## 9. Commercial Offer Registry (17 Day-One Rows in `entitlement_offers`)

| Row Key | Price | Model |
|---|---|---|
| `county_rank` | Free | Speed & Market Rank Report |
| `first_appointment_free` | Free | First attended meeting credit |
| `appt_owner_1_4` | $175/event | 1–4 door appointments |
| `appt_owner_5_9` | $350/event | 5–9 door appointments |
| `appt_owner_10plus` | $500/event | 10+ door appointments |
| `appt_seat_a_rate` | $125/event | Seat A holder discount |
| `appt_pack_8` | $1,200 pack | 8-appt bulk drawdown |
| `appt_standing_order` | $320/mo | Monthly standing order (2 appts/mo) |
| `appt_principal` | $750/event | 20–80 door principal |
| `appt_second_pass` | $75/event | Pre-qualified lead ≥90d prior |
| `appt_deadbook` | $75/event | Dead-book reactivated |
| `respond` | $249/mo | Inbound concierge (Speed-to-Lead) |
| `retention_guard` | $497/mo | Client book churn monitoring |
| `growth_os` | $897/$997/mo | Door Growth Operating System |
| `rent_gap_report` | $197/mo | Under-market rent identification |
| `owner_report_card` | $197/mo | Quarterly branded owner PDF report |
| `seat_a_door_gen` | $999→$1,999/mo | First-refusal on county leads |

---

## 10. Billing Model

- **Zero upfront fees.** Payment auth captured at onboarding (Stripe Elements card + ACH mandate).
- **50/50 split:** 50% at appointment signature, 50% at Day 60 (nightly PMS verification).
- **60-Day Clawback:** If agreement cancelled within 60 days, second installment auto-voided in Stripe.
- **4-Rule Verification Bar** must pass before any invoice is generated.
- **Retention Floor:** If 90-day signed-to-attended conversion ≥15%, no goodwill credits owed.

---

## 11. Key Architectural Components

### Ghost-Shopper Audit Factory
Headless crawler submits standardized owner inquiry to target PM website → captures response latency (milliseconds) → calculates annual revenue loss PDF → injects Sendspark video merge tokens → dispatches Email 1.

### Outbound Sequence (5-Touch)
| Touch | Channel | Day | Content |
|---|---|---|---|
| 1 | Cold Email | 0 | Speed Loss Audit + Sendspark video |
| 2 | Phone Call (Slack task) | 1–2 | Audit follow-up |
| 3 | Cold Email | 4 | Fee-Stack Opportunity |
| 4 | LinkedIn (manual) | 7 | Executive peer networking |
| 5 | Cold Email | 10 | Metro Speed Index & scarcity |
| Conditional | SMS nudge | Post-engage | Direct scheduling nudge (engaged only) |

### Reply Triage Agent Intent Classes
`HOT_LEAD`, `QUESTION`, `OBJECTION`, `LATER`, `NURTURE`, `UNSUBSCRIBE`, `COMPLAINT`, `LEGAL_GRIEF`, `WHALE_OWNER`, `PARTNER`

### Preflight Gates (all 10 must be green to arm campaigns)
GATE-01: Stripe auth, GATE-02: PM profile complete, GATE-03: Territory defined, GATE-04: Calendar slots, GATE-05: Historical data ingested, GATE-06: Documents uploaded, GATE-07: Partner menu reviewed, GATE-08: Domains SPF/DKIM/DMARC valid, GATE-09: Non-poach suppression active, GATE-10: Live-fire roundtrip <60s.

---

## 12. System Reliability

| Failure | Detection | Recovery |
|---|---|---|
| Third-party API outage | 3x 5xx / timeout | Graceful fallback cache; DEGRADED label; exponential backoff |
| Stale data / missing key | >30d or null key | Returns `UNKNOWN`/`ABSTAIN`; alerts `#blackink-qa` |
| Persistent task failure | 3 retry attempts | Dead-Letter Queue; priority incident in Slack |
| Domain reputation drop | Bounce >3% or spam >0.08% (48hr window) | Auto-quarantine; route to warmed backup domain |
| Cross-tenant isolation breach | Adversarial test detects conflicting `client_id` | Emergency Circuit Breaker; freeze dispatch; P0 alert `#blackink-command` |
| Emergency operator halt | `/halt` command in Slack | Persistently freeze all schedulers + message queues in Redis/DB until explicit resume |
