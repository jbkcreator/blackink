# Blackink Week 0 — Dev Task Split
**Aug 31 – Sept 2, 2026 | 4 Developers | 10DLC filing excluded (handled separately)**

---

## Dev 1 — Data Infrastructure & Pipeline

**Owner:** Akrash handoff + multi-tenant schema

### From 3.0.1 (Reuse Audit)
- **Pattern adoption:** Establish row-level `client_id` tenant keying across all primary tables, views, and Redis cache keys

### From 3.0.1 (Refactored)
- **Data Normalization & Ingestion:** Adapt entity resolution routines to distinguish generic property contacts from corporate LLC owners holding multi-unit portfolios

### From 3.0.5 (Akrash Staging Handoff)
- Provision `raw_prospect_pipeline` PostgreSQL staging table with full schema:
  - Required fields: `company_id`, `company_name`, `domain`, `market_metro`, `door_count_est`, two contacts (`OWNER_BROKER_MD` or `OFFICE_MANAGER_OPS`), verified email, direct phone, audit timestamps
- Implement quarantine gate: records stay locked until email verification, DNC clearance, global opt-out check, and non-poach cross-suppression all pass
- Provision Akrash restricted write access to staging table
- Delegate DNS access; configure SPF, DKIM, DMARC across 20 domains (40 mailboxes) and initialize warmup schedules

### Acceptance Criteria Owned
- **AC #6:** Signed data interface spec approved, `raw_prospect_pipeline` schema live in staging

---

## Dev 2 — Platform Defect Remediation

**Owner:** All four 3.0.2 fixes

### A. Vera Silent-Zero Remediation
- Refactor data-reconciliation layer to enforce strict tri-state logic: `VALUE` / `UNKNOWN` / `ABSTAIN`
- When Stripe, calendar logs, or PM software read feeds are unreachable or return missing values → emit `UNKNOWN`/`ABSTAIN`, halt downstream settlement, alert admins
- Zero silent `0` returns permitted

### B. Relay Persistent Halt & TTL Re-Arm Patch
- Update scheduler and queue worker state machines so global, client-level, and campaign-level pause states persist in both Redis and PostgreSQL
- Remove all TTL-based auto-resume logic
- Queues remain locked until an authorized admin issues a cryptographic resume command via Slack

### C. Cora Queue Throttling & Batch Burst Protection
- Embed strict queue bounds and pacing throttles into draft orchestrators
- Auto-pause new draft generation once unreviewed Slack approval queue hits capacity
- Auto-resume only as human reviews clear the backlog

### D. Standalone Hunter Entity Resolution Worker
- Provision a standalone worker droplet (separate from main API/web threads)
- Implement nightly async background sweeps resolving corporate names, registered agents, and individual property owners across fragmented multi-property LLC portfolios
- No cross-table joins on the primary application DB

### Acceptance Criteria Owned
- **AC #2:** Vera health jobs output `UNKNOWN`/`ABSTAIN` on missing inputs — zero silent-zero returns across staging
- **AC #3:** Emergency pause triggered via Slack persists across server restarts; only authorized resume clears it
- **AC #4:** Hunter standalone workers resolve a sample batch of property owner entities across fragmented LLCs

---

## Dev 3 — Slack Agent Hub (@Blackink)

**Owner:** Full @Blackink interactive cockpit

### From 3.0.1 (Direct Port)
- **Event-Driven Execution Harnesses:** Port async queue runners, dispatchers, and state machines
- **Slack Interactive State Machines:** Port Block Kit button handlers (`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`) and modal submission listeners

### From 3.0.3 (Net New)
- Stand up `@Blackink` Slack app as centralized router (command & intent dispatcher)
- Configure all five dedicated operational channels:
  - `#blackink-command` — executive overview, macro pipeline queries, global pause/resume controls
  - `#blackink-setter` — 1-screen context cards for high-intent owner leads
  - `#sales-replies` — inbound reply stream with automated intent tags
  - `#dial-tasks` — prioritized daily phone queue (company background, response latency, direct line)
  - `#blackink-qa` — system health logs, API heartbeat failures, domain reputation deltas, cross-tenant leakage alerts
  - `#blackink-economics` — CAC rollups, channel unit economics, wallet cap dashboards
- Implement payload-bound SHA-256 hash verification on every interactive card: bind each card to a hash of its exact message payload + recipient identifier + config state; reject clicks on stale/modified cards

### Acceptance Criteria Owned
- **AC #1:** @Blackink app active in workspace, posting native action cards and processing button clicks with hash verification

---

## Dev 4 — Compliance Gates, Suppression & Reuse Ledger

**Owner:** DNC/suppression, outbound compliance, and final Reuse Ledger doc

### From 3.0.1 (Direct Port)
- **Outbound Drafting Patterns:** Port LLM prompt templates and structured output formatters from prior drafting engines
- **Suppression & DNC Scrubbing:** Port deterministic matching routines connecting to state and national DNC registries

### From 3.0.1 (Refactored)
- **Outbound Dispatch Adapters:** Decouple pooled sending identities into strict per-tenant mailbox assignments (no cross-tenant bleed)
- **Audit Compilation Pipelines:** Modularize PDF loss-report generators to consume property management speed metrics instead of general distress scoring

### From 3.0.1 (Pattern Adoption)
- **Deterministic Gate Enforcement:** Hard-code pre-send policy checks that fully bypass LLMs when evaluating suppression, quiet hours, and channel eligibility
- CI/CD gate: build fails if code attempts outbound SMS to any contact where `inbound_sms_count = 0` AND `booked_appointment_id IS NULL`
- Enforce quiet hours: no SMS delivery 9 PM – 8 AM recipient local time
- Enforce `{client_firm}` tag requirement on all outbound templates

### Reuse Ledger (3.0.1 Deliverable)
- Compile and deliver the formal Reuse Ledger at Week 0 close:
  - Components ported (direct fork)
  - Architectural refactors completed
  - Pattern-only adoptions
  - Automated test pass rates per module

### Acceptance Criteria Owned
- **AC #5:** CI/CD tests blocking cold outbound SMS are live and passing
- **AC #7:** Complete module-by-module Reuse Ledger delivered with test coverage documented

---

## Parked — Not Dev-Assigned

| Item | Owner | Notes |
|------|-------|-------|
| A2P 10DLC Brand + Campaign Registration (3.0.4) | Non-dev / compliance | Filed via Telnyx/TCR portal; legal entity HEU AI LLC; mixed use case (Customer Care + Account Notification) |

---

## Acceptance Criteria Summary

| AC | Owner |
|----|-------|
| #1 — Live Slack cockpit with hash verification | Dev 3 |
| #2 — Vera zero silent-zero returns | Dev 2 |
| #3 — Persistent halt survives restarts | Dev 2 |
| #4 — Hunter entity resolution batch passes | Dev 2 |
| #5 — CI/CD SMS gate live | Dev 4 |
| #6 — Signed data contract + staging schema live | Dev 1 |
| #7 — Reuse Ledger delivered | Dev 4 |
| A2P 10DLC submitted | Parked |
