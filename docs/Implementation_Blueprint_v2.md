# UNIFIED SYSTEM SPECIFICATION & IMPLEMENTATION BLUEPRINT (v2 — Active Scope)

## Project Blackink — Implementation Blueprint

**Client:** Josh Kantor, Blackink
**Lead Developer:** Hari Krishnan (heu.ai)
**Document Version:** v2 — Source of Truth applied Sep 3 2026. Active scope only.
**Archived original:** `Project_Blackink_-_Complete_Implementation_Blueprint__Full_.md`

> **Scope note:** This document contains only features approved for the September build. Omitted: Ghost Shopper (prohibited — no pretext inquiries), Sendspark/video (deferred — no account), Rent Analysis Bot (deferred to Q1 — no API contracts), async-close path (not approved), outbound SMS (deferred this year), Calendly (replaced by Google Calendar / Microsoft Graph + GHL fallback), Twilio (replaced by Telnyx), metro layer (replaced by county). See the original file for the full historical archive.

---

## 1. Master Data Contracts & Ingestion Variable Specification

All upstream prospect and market intelligence delivered by the data pipeline must map directly to the `raw_prospect_pipeline` staging schema before ingestion into the primary database.

### A. Target Company & Market Entity Schema

- **company_id** (UUID, Primary Key): Generated UUID. NOTE: The source also describes a SHA-256 domain-derived identifier; this conflict must be resolved explicitly before implementation — do not merge PM firm and property owner identities.
- **company_name** (String, NOT NULL): Legal operating or DBA name of the property management company.
- **website** (String, NOT NULL): Validated corporate website URL.
- **domain** (String, UNIQUE, NOT NULL): Normalized apex domain (e.g., `suncoastpm.com`).
- **county** (String, NOT NULL): Geographic target county (e.g., `Hillsborough`, `Pinellas`, `Orange`). The county is the unit of data, ranking, contract, and seat — there is no metro layer. Launch priority: Hillsborough and Pinellas first (reusing existing Forced Action records). Next: Orange, Duval, Polk, Pasco, Lee, Brevard, Volusia, Seminole. Miami-Dade, Broward, Palm Beach are deliberately not first-launch counties.
- **door_count_est** (Integer, NOT NULL): Estimated residential doors under management.
- **current_pm_software** (String, Nullable): Ingested primary PMS platform (`AppFolio`, `Buildium`, `Propertyware`, `Rent Manager`, `Other`, `UNKNOWN`).

### B. Two-Contact Structure (Owner/Broker & Operations)

- **contact_id** (UUID, Primary Key): Unique identifier per contact.
- **contact_role_type** (Enum, NOT NULL): Explicit role tag (`OWNER_BROKER_MD` for Contact A; `OFFICE_MANAGER_OPS` for Contact B).
- **first_name** (String, NOT NULL): Contact given name.
- **last_name** (String, NOT NULL): Contact family name.
- **title** (String, NOT NULL): Full professional title.
- **email** (String, NOT NULL): Direct corporate email address.
- **email_status** (Enum, NOT NULL): `VERIFIED`, `ESTIMATED`, `UNVERIFIED`, `BOUNCED`.
- **phone** (String, E.164 format, Nullable): Direct line or office telephone (`+1XXXXXXXXXX`).
- **phone_type** (Enum, NOT NULL): `MOBILE`, `DIRECT_WORK`, `OFFICE_LANDLINE`.
- **linkedin_url** (String, Nullable): Personal LinkedIn profile URL.

### C. Data Lineage & Provenance Metadata

- **source_channel** (String, NOT NULL): Originating raw list source (`FL_DBPR_LICENSE`, `GOOGLE_MAPS_SWEEP`, `MANUAL_CURATED`).
- **source_timestamp** (ISO 8601 UTC, NOT NULL): Extraction datetime.
- **enrichment_timestamp** (ISO 8601 UTC, NOT NULL): Verification datetime.
- **enrichment_provider** (String, NOT NULL): Validation provider (`Hunter`, `Anymail`, `Tracerfy`, `Internal_Scraper`).

### D. Audit & Personalization Payload

- **audit_loss_dollars_est** (Integer, Nullable): Modeled annual revenue loss based on Owner Visibility Score public-signal gap and door count.
- **custom_hook_text** (String, Nullable): Dynamic personalized opening hook for Email 1.

### E. Compliance & Suppression Attributes

- **suppression_state** (Boolean, DEFAULT FALSE): Global suppression flag (`TRUE` = locked from all outbound).
- **dnc_clean** (Boolean, DEFAULT FALSE): Verified against National and Florida DNC registries.
- **is_opted_out** (Boolean, DEFAULT FALSE): Explicit opt-out recorded across any channel.
- **compliance_eligibility** (Enum, NOT NULL): `EMAIL_COLD_ELIGIBLE`, `TRANSACTIONAL_SMS_ONLY`, `BLOCKED`.

### Ingestion Validation & "Ready for Campaign" Gate

A staged record automatically transitions to `READY_FOR_CAMPAIGN` only when all predicate checks pass:

```sql
-- Execution Gate in src/compliance/gate_evaluator.py
-- Ghost-shopper score and video ID prerequisites removed per Source of Truth Sep 3 2026
CREATE OR REPLACE FUNCTION evaluate_campaign_readiness(target_contact_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
RETURN EXISTS (
  SELECT 1 FROM raw_prospect_pipeline p
  JOIN companies c ON c.domain = p.domain
  WHERE p.contact_id = target_contact_id
  AND p.email_status = 'VERIFIED'
  AND p.is_opted_out = FALSE
  AND p.dnc_clean = TRUE
  AND p.suppression_state = FALSE
  AND p.compliance_eligibility = 'EMAIL_COLD_ELIGIBLE'
  AND NOT EXISTS (
    -- Cross-client non-poach gate check
    SELECT 1 FROM client_pm_books b
    WHERE b.owner_domain = p.domain OR b.owner_email = p.email
  )
  AND (
    p.last_outbound_touch_at IS NULL
    OR p.last_outbound_touch_at <= NOW() - INTERVAL '14 days'
  )
);
END;
$$ LANGUAGE plpgsql;
```

## 2. Core Architectural Principles & Invariants

- **Commercial Terms as Database Rows:** No price, fee, tier, seat, escalator, credit, cap, or gate is compiled into an agent or hardcoded into application branches. Adding or updating any commercial offer is strictly an INSERT into the `entitlement_offers` table.
- **The Outcome Table as System of Record:** The generic events and outcomes datastores are constructed first. Every learning loop, offer trigger, dispute evaluation, audit trail, and communication log reads from or writes to this shared ledger.
- **Engine Architecture (Row + Adapter):** Every revenue engine is modeled as a database configuration row coupled to a dedicated execution adapter.
- **Deterministic Money and Compliance:** No autonomous agent or LLM makes financial, billing, legal, or compliance decisions. The compliance gate, non-poach suppression, dispute rules, and Stripe settlement pipelines run on strict, unbypassable code.
- **Tenant Isolation & Security:** Data, sending reputation, credentials, and memory structures are strictly partitioned by `client_id`. Cross-client leakage tests run automatically in CI/CD.
- **Human Approval for Early-Client Sends:** Every email dispatch in the early client phase requires human approval in `#blackink-setter` before the message is sent. A template class earns autonomous dispatch (Band 2) only after 50 consecutive clean approved sends.

## 3. Master Week-by-Week Implementation Sprints

### Week 0 (Aug 31 – Sept 2, 2026): Step 1 Reuse, Critical Platform Remediation & Compliance

**Phase Objective:** Gate 1 Proof, Fork & Baseline Stabilization

Week 0 serves as the foundational validation gate for the entire Blackink operating platform. Before deploying net-new campaign logic, commercial sequences, or client-facing onboarding tools, the technical team isolates and audits all reusable infrastructure across prior builds. This phase forks core execution pipelines, eliminates known legacy defects, deploys the centralized Slack Agent Hub, establishes the upstream data pipeline contract with Akrash, and files official carrier brand and campaign registrations. Week 0 operates as a strict proof gate: work focuses on establishing tenant isolation, persistent safety halts, deterministic compliance gates, and verified truth states so that downstream marketing and settlement modules build upon a stabilized, bug-free platform.

#### 3.0.1 Core Asset Audit & Module-by-Module Reuse Ledger

**Direct Porting (Clean Fork):**

- Outbound Drafting Patterns: Language models and structured output formatters from drafting engines.
- Event-Driven Execution Harnesses: Asynchronous queue runners, dispatchers, and state machines.
- Suppression & DNC Scrubbing: Deterministic matching routines connecting to state and national Do-Not-Call registries.
- Slack Interactive State Machines: Interactive Block Kit button handlers (`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`) and modal submission listeners.

**Refactored Components:**

- Data Normalization & Ingestion: Adapting entity resolution routines to separate generic properties from corporate LLC owners holding multi-unit portfolios. County field replaces metro.
- Outbound Dispatch Adapters: Decoupling pooled sending identities into strict, tenant-isolated mailbox assignments.
- Audit Compilation Pipelines: Modularizing PDF report generators to consume Owner Visibility Score public-signal data rather than ghost-shopper latency metrics.

**Pattern-Only Adoptions:**

- Multi-Tenant Data Schema: Establishing row-level tenant keying (`client_id`) across all primary tables, views, and Redis cache keys.
- Deterministic Gate Enforcement: Hard-coded pre-send policy checks that completely bypass LLMs when evaluating suppression, quiet hours, and channel eligibility.

A formal Reuse Ledger is compiled and delivered at the conclusion of Week 0.

#### 3.0.2 Platform Defect Remediation & Core Infrastructure Hardening

**A. Vera Silent-Zero Remediation:** The data-reconciliation layer is refactored to enforce strict tri-state logic. When an external integration returns missing values, the system explicitly returns `UNKNOWN` or `ABSTAIN`. Downstream settlement routines halt automatically and alert administrators rather than assuming zero payable activity.

**B. Relay Persistent Halt & TTL Re-Arm Patch:** The scheduler and queue worker state machines are updated so that a global, client-level, or campaign-level pause persists indefinitely in Redis and PostgreSQL. Outbound queues remain locked until an authorized administrator explicitly executes a cryptographic resume command in Slack. A TTL or restart must never re-arm a paused campaign.

**C. Cora Queue Throttling & Batch Burst Protection:** Strict queue bounds and pacing throttles are embedded into draft orchestrators. Generation limits automatically pause new drafting once unreviewed Slack approval queues reach capacity (50 unreviewed drafts or any unreviewed draft older than 24 hours), resuming only as human reviews clear items.

**D. Standalone Hunter Entity Resolution Deployment:** A standalone worker droplet is provisioned exclusively for Hunter-style entity matching. It runs nightly asynchronous background sweeps, resolving corporate names, registered agents, and individual property owners across fragmented multi-property LLC portfolios.

#### 3.0.3 Slack Agent Hub (@Blackink) & Interactive Cockpit Deployment

```
CENTRAL SLACK AGENT HUB ARCHITECTURE
┌──────────────────────────────┐
│      @Blackink Router        │
│ (Command & Intent Dispatcher)│
└──────────────┬───────────────┘
               │
┌─────────────────┬──────────────┼───────────────┬─────────────────┐
▼                  ▼              ▼               ▼                 ▼
#blackink-command  #blackink-setter  #sales-replies  #dial-tasks  #blackink-qa &
(Global Cockpit)   (Context Cards)   (Inbound Triage)(Call Tasks) #blackink-economics
```

**Dedicated Operational Channels:**

- `#blackink-command`: Executive overview, macro pipeline queries, active tenant statuses, and global pause/resume controls.
- `#blackink-setter`: Human conversation cockpit rendering 1-screen context cards for high-intent owner leads. **Also the approval queue for all outbound email drafts in the early client phase** — every email touch requires an Approve click before dispatch.
- `#sales-replies`: Real-time inbound reply stream routing prospect email responses with automated intent tags.
- `#dial-tasks`: Prioritized daily phone queue populated with company background, Owner Visibility Score, and direct lines.
- `#blackink-qa`: Real-time system health logs, API heartbeat failures, domain reputation deltas, and cross-tenant leakage test alerts.
- `#blackink-economics`: Rollup dashboards tracking customer acquisition costs, channel unit economics, and wallet caps.

**Payload-Bound Hash Verification:** Every interactive Slack card (`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`) is cryptographically bound to a SHA-256 hash of the exact message payload, recipient identifier, and configuration state. If a draft payload or template is modified in the background while awaiting review, clicking an outdated Slack button is rejected by the backend.

#### 3.0.4 A2P 10DLC Carrier Registration & Compliance Infrastructure

**Note (Source of Truth Sep 3 2026):** No SMS this year. 10DLC registration is filed for future readiness but is not a September launch dependency. No cold outbound SMS and no transactional SMS until further notice.

**Brand Registration:** Legal Entity: HEU AI LLC. Address: 971 US Highway 202N Ste N, Branchburg, NJ 08876. Entity Type: Private Company, LLC (New Jersey). Vertical: Real Estate / Professional Services.

**Campaign Filing Details:** Use Case Classification: Customer Care + Account Notification (non-marketing). Campaign Description retained for future filing. Opt-in and consent language preserved. Keywords: `STOP`, `END`, `CANCEL`, `UNSUBSCRIBE`, `QUIT` for automated opt-out; `HELP` for support routing.

**Code-Level Cold SMS Enforcement (Safety Layer — Active Now):** CI/CD build test fails immediately if code attempts an outbound SMS to any contact where `inbound_sms_count == 0 AND booked_appointment_id IS NULL`. This hard block remains active as a safety gate regardless of the no-SMS-this-year rule.

#### 3.0.5 Data Pipeline Interface & Akrash Staging Handoff

Week 0 finalizes the formal operational data contract between the upstream data team (Akrash) and the core platform (Hari):

- **Staging Database Ingestion:** Akrash is provisioned restricted access to write prospect data directly into the `raw_prospect_pipeline` PostgreSQL staging table.
- **Mandatory Schema Fields:** `company_id`, `company_name`, `domain`, `county`, `door_count_est`, two contacts (`contact_role_type` as `OWNER_BROKER_MD` or `OFFICE_MANAGER_OPS`), verified email, direct phone, and audit timestamps. Map staging fields explicitly to blueprint storage names; return rejected rows with reason codes, never silently drop them.
- **Ready for Campaign Gate:** Upstream records remain in quarantine until background evaluators confirm email verification, DNC clearance, global opt-out clearance, and non-poach cross-suppression checks.
- **DNS & Warmup Delegation:** DNS access is delegated to configure SPF, DKIM, and DMARC across 20 dedicated domains (40 mailboxes), initializing domain warmup schedules ahead of campaign launch.

#### 3.0.6 Week 0 Acceptance Criteria & Definition of Done

1. **Live Slack Cockpit:** The @Blackink Slack app is active, posting native action cards and processing interactive button clicks with payload-bound hash verification.
2. **Defect-Free Health Reporting:** Vera health jobs output `UNKNOWN` or `ABSTAIN` states on missing inputs with zero silent zero returns.
3. **Verified Persistent Halt:** An emergency pause triggered via Slack persistently halts background execution queues across server restarts until an authorized resume is executed.
4. **Entity Resolution Pipeline:** Hunter standalone workers successfully resolve a sample batch of property owner entities across fragmented LLCs.
5. **Submitted A2P 10DLC Filing:** Brand and Campaign registrations submitted to TCR (not a September SMS dependency). Automated CI/CD tests blocking cold outbound SMS verified.
6. **Signed Data Interface Contract:** The Data Interface Specification is approved, and the `raw_prospect_pipeline` database staging schema with `county` field is live.
7. **Documented Reuse Ledger:** Complete module-by-module accounting of ported assets, architectural refactors, and test coverage delivered.

---

### Week 1 (Sept 1 – Sept 11, 2026): Sprint 1 — Phase 0 + Marketing & Demo Layer

**Milestone Standard:** September 11 Marketing Live (Live Outbound Campaigns, Owner Visibility Score Reports, Automated Booking, and Demo Kit Sandbox)

Week 1 transitions Blackink from foundational scaffolding into a live demand-generation engine. The primary objective is to make the outbound sales and marketing systems fully functional by September 11. The build sequence decouples frontend marketing and demo assets from downstream tenant settlement rails.

```
WEEK 1 COMPLETE CAMPAIGN & DEMO LAYER ARCHITECTURE (GO-LIVE: SEPTEMBER 11, 2026)

[Raw Prospect Data: 500-1,000 PMs] ──► [Deterministic Compliance Gate] ──► [Owner Visibility Score Engine]
        │                                                                          │
[Non-Poach / DNC / Waterfall]                                            [Public Observable Signals]
        │                                                                          │
        ▼                                                                          ▼
[Personalized Outbound Sequences] ◄── [Owner Visibility Score PDF] ◄── [County Rank Calculator]
        │
        ├──► [Email 1: OVS Report + Reply-YES Micro-Ask ──► Human Approval in #blackink-setter]
        ├──► [Day 1–2 Phone Call Task ──► Enqueued into Slack #dial-tasks]
        ├──► [Email 3: Fee-Stack Revenue Opportunity Map (Standalone PDF)]
        ├──► [LinkedIn Deep-Link Handoff ──► Manual Clipboard Copy]
        └──► [Email 5: County Visibility Rank & Market Angle]
        │
        ▼
[Inbound Reply Bridge ──► Real-Time Slack #sales-replies]
        │
        ▼
[Google Calendar / Microsoft Graph Direct Booking ──► Show-Rate Reminder Cascade]
        │
        ▼
[Pre-Demo Lead-In Email (30 min Prior) — Owner Visibility Score PDF attached]
        │
        ▼
[Sales Demo Kit: Permanent Golden Client Sandbox Live Rehearsal]
```

#### 3.1.1 Data Spine, Entity Models & Ingestion Pipeline

```sql
-- Location: src/db/migrations/001_core_spine.sql
-- 1. GENERIC EVENTS STREAM (Shared Ledger of Record)
CREATE TABLE events (
  event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  owner_id UUID,
  property_id UUID,
  campaign_id UUID,
  event_type VARCHAR(100) NOT NULL,
  source VARCHAR(50) NOT NULL,
  value_cents BIGINT DEFAULT 0,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  occurred_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. TARGET COMPANIES (Property Management Firms)
CREATE TABLE companies (
  company_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  domain VARCHAR(255) UNIQUE NOT NULL,
  company_name VARCHAR(255) NOT NULL,
  website VARCHAR(255) NOT NULL,
  county VARCHAR(100) NOT NULL,
  door_count_est INTEGER NOT NULL DEFAULT 0,
  current_pm_software VARCHAR(100) DEFAULT 'UNKNOWN',
  status VARCHAR(50) DEFAULT 'PROSPECTING',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. CONTACTS (Two-Contact Ingestion Model)
CREATE TABLE contacts (
  contact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  contact_role_type VARCHAR(50) NOT NULL,
  first_name VARCHAR(100) NOT NULL,
  last_name VARCHAR(100) NOT NULL,
  title VARCHAR(255) NOT NULL,
  email VARCHAR(255) NOT NULL,
  email_status VARCHAR(50) NOT NULL DEFAULT 'UNVERIFIED',
  phone VARCHAR(50),
  phone_type VARCHAR(50) DEFAULT 'OFFICE_LANDLINE',
  linkedin_url TEXT,
  is_opted_out BOOLEAN DEFAULT FALSE,
  dnc_clean BOOLEAN DEFAULT FALSE,
  suppression_state BOOLEAN DEFAULT FALSE,
  compliance_eligibility VARCHAR(50) DEFAULT 'BLOCKED',
  last_outbound_touch_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. PM PROFILE
CREATE TABLE pm_profiles (
  profile_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID UNIQUE NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  specialty_tags TEXT[] DEFAULT '{}',
  languages_supported TEXT[] DEFAULT '{"English"}',
  asset_class_strengths TEXT[] DEFAULT '{"Single Family", "Small Multifamily"}',
  geographic_coverage_polygon JSONB,
  historical_close_rate NUMERIC(5,2) DEFAULT 0.00,
  average_speed_to_lead_seconds INTEGER DEFAULT 0,
  show_rate_percentage NUMERIC(5,2) DEFAULT 0.00,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_events_client_type ON events(client_id, event_type, occurred_at);
CREATE INDEX idx_contacts_lookup ON contacts(email, company_id, compliance_eligibility);
CREATE INDEX idx_companies_domain ON companies(domain);
```

The ingestion pipeline processes 500–1,000 target property management companies. Launch priority counties: **Hillsborough and Pinellas** (reusing existing Forced Action records). Next wave: Orange, Duval, Polk, Pasco, Lee, Brevard, Volusia, Seminole. Miami-Dade, Broward, and Palm Beach are deliberately not first-launch counties.

For every target company, the pipeline resolves two distinct contacts:

1. **Contact A (`OWNER_BROKER_MD`):** The ultimate decision-maker (Managing Broker, Owner, President, CEO).
2. **Contact B (`OFFICE_MANAGER_OPS`):** The operational gatekeeper (Operations Manager, Lead Property Manager).

#### 3.1.2 Deterministic Compliance Gate & Non-Poach Architecture

```
DETERMINISTIC COMPLIANCE & WATERFALL FLOW

[Prospect Record Enters Execution Gate]
        │
        ▼
┌──────────────────────────────────────┐
│ 1. Global / Explicit Opt-Out Check   │ ──► [FAILED] ──► [PERMANENTLY BLOCKED]
└──────────────────┬───────────────────┘
        │ [PASSED]
        ▼
┌──────────────────────────────────────┐
│ 2. Cross-Client Non-Poach Validation │ ──► [MATCH] ──► [SUPPRESSED & LOGGED]
└──────────────────┬───────────────────┘
        │ [PASSED]
        ▼
┌──────────────────────────────────────┐
│ 3. DNC Registry & Quiet Hours Check  │ ──► [FAILED] ──► [CHANNEL SUPPRESSED]
└──────────────────┬───────────────────┘
        │ [PASSED]
        ▼
┌──────────────────────────────────────┐
│ 4. Warm-Channel Waterfall Routing    │
└──────────────────┬───────────────────┘
        │
┌─────────────────────────┴─────────────────────────┐
▼                                                     ▼
[Cold Outbound Tier]                          [Engaged / Booked Tier]
- Email: ELIGIBLE (Verified Email Only)       - Email: ELIGIBLE
- Human Phone: Task to #dial-tasks            - (SMS: deferred this year)
- Cold SMS: STRICTLY BANNED (CI Fails Build)
```

**Warm-Channel Waterfall:** Cold prospect outreach is strictly restricted to email and human telephone tasks. Cold outbound SMS is blocked at the database, application, and CI/CD testing levels. No outbound SMS this year.

**Cross-Client Non-Poach Gate:** Read-only connections to active clients' property management software sync current owner rosters into a centralized suppression table.

**County Allocation Algorithm:** Where multiple property management firms operate within the same county boundary, target owners are allocated to one client campaign at a time. If outreach remains unacted upon for 30 days, allocation re-evaluates via an automated timer.

**National & State DNC Scrubbing:** Automated pre-send linter queries real-time DNC registries, stripping dial tasks from restricted records.

#### 3.1.3 Owner Visibility Score Engine & Report Generation

The Owner Visibility Score replaces ghost-shopper response auditing. All signals are drawn from publicly accessible sources only — no pretext inquiries, no form submissions, no automated contact with the target firm.

```
OWNER VISIBILITY SCORE ENGINE

[Target PM Website + Business Profiles + FL DBPR Licence Rolls]
        │
        ▼
[Public Signal Scraper: 10-Category Observable Data Collection]
        │
        ▼
[Score Calculator: 0–100 Points Across Categories]
        │
        ▼
[County Rank Calculator] ──► [Top 25 per county published; no bottom list]
        │
        ▼
[Owner Visibility Score PDF Report Compiler]
        ├──► Page 1: Score, County Rank, Data Coverage %, 3 Lowest Named Comparisons
        └──► Page 2: Revenue Model (8% mgmt fee / 30-month tenure / ~$100/door/month)
        │
        ▼
[Outbound Dispatch: OVS PDF attached to Email 1 payload ──► Queued for Human Approval]
```

**Owner Visibility Score — 10-Category Public Observable Rubric:**

| Category                   | Signal                                                  | Maximum Points |
| -------------------------- | ------------------------------------------------------- | -------------: |
| Owner conversion readiness | Separate owner-addressed page                           |             14 |
| Owner conversion readiness | Working owner contact form, phone, and email            |             10 |
| Owner conversion readiness | Mobile, HTTPS, load under 3 seconds                     |              6 |
| Market visibility          | Review count against county median                      |             16 |
| Market visibility          | Review recency: under 60 days full; over 12 months zero |             12 |
| Public reputation          | Review response rate                                    |             18 |
| Public reputation          | Average rating                                          |              8 |
| Accessibility              | Published after-hours contact route                     |              8 |
| Accessibility              | Median review response lag                              |              4 |
| Credibility                | Active broker licence and tenure                        |              4 |
| **Total**            |                                                         |  **100** |

Sources: Google/Yelp business profiles, the firm's website, and FL DBPR broker licence rolls. Display data coverage with the score (e.g., "76/100, 94% data coverage"). Score cached monthly per firm; alert job diffs against last month when delta >10 points. Top 25 per county published only; never a bottom list. Below the data floor (fewer than 3 scored signals): show "insufficient data" and no rank.

**A. Public Signal Scraper & Score Calculator** — For each of the 10 signal categories, the scraper returns a signal value and a `data_coverage` flag. Missing signal → 0 points for that category, flagged as `MISSING_DATA` in the payload, not a fatal error. Final score and per-category breakdown written to `events` with `event_type = 'owner_visibility_score_calculated'`.

**B. County Rank Calculator & Peer Benchmarking** — County rank computed as percentile across all scored firms in the same county. Named peer comparisons: identify 2–3 real named competitors in the same county with higher scores on the target's three weakest categories. These comparisons populate both the PDF report and the setter context card.

**C. Owner Visibility Score PDF Report Compiler** — Branded 2-page executive PDF:

- Page 1: Score (large visual), county rank, data coverage %, three lowest-scoring observations with named peer comparisons
- Page 2: Revenue model using: `Lost Revenue = (Monthly Leads × (1 − e^(−0.0005 × avg_county_response_gap_sec))) × (Avg Monthly Fee × 12) × Owner Tenure`
- Default model inputs (overridable per client): 8% management fee, 30-month average owner tenure, ~$100/door/month. All estimates clearly labelled as estimates; assumptions displayed on the report.
- Branded with `getblackink.com`; prospect `company_name` on cover header
- Stored on object storage; URL written to contact record; `score_pdf_generated` event logged

**D. Fee-Stack One-Pager Generator (ADD-8-Lite — Standalone)** — Discovery proof artifact mapping uncollected fee lines and ancillary margins. 1-page PDF highlighting minimum 5 fee leakage categories (lease renewal fees, maintenance markups, tenant setup fees, pet rent share, resident benefits packages) with estimated annual uplift per category. Uses the shared merge-tag template pipeline. Attached to Touch 3 (Day 4 email).

#### 3.1.4 Multi-Touch Outbound Sequencer & Human Bridge

| Sequence Step | Channel & Mechanism           | Timing   | Content Focus                                                                                                      | Governance Rule                                                                                                                    |
| ------------- | ----------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| Touch 1       | Cold Email (Direct)           | Day 0    | Owner Visibility Score PDF + reply micro-ask ("Reply YES to see where you rank in [County]")                       | **Human Slack approval required in #blackink-setter before dispatch.** Verified corporate email only; tracking pixel active. |
| Touch 2       | Human Phone Call (Slack Task) | Day 1–2 | Follow-up call: setter references OVS score, county rank, and 3 weakest signal categories.                         | Verified direct line; local calling hours enforced.                                                                                |
| Touch 3       | Cold Email (Direct)           | Day 4    | Fee-Stack One-Pager: introduces the ADD-8-Lite fee analysis showing uncollected ancillary revenue.                 | **Human approval required.** Threaded to Email 1; verifies no prior opt-out or reply.                                        |
| Touch 4       | LinkedIn Deep-Link (Manual)   | Day 7    | Executive peer networking: generates target profile URL and copies tailored connection note to setter's clipboard. | Logged as manual task; no headless browser automation.                                                                             |
| Touch 5       | Cold Email (Direct)           | Day 10   | County Visibility Rank & Scarcity: references firm's county rank and upcoming territory availability.              | **Human approval required.** Final cold email touch before entering 30-day cooling.                                          |

**Human Approval Gate:** Every email touch in the early client phase requires a Slack approval card in `#blackink-setter`. The sequencer queues a draft; the email is NOT sent until a rep clicks Approve. A template class earns autonomous dispatch (Band 2) after 50 consecutive clean approved sends.

**Interim Reply Bridge (September 11–16):** Until the automated Reply Triage Agent deploys in Week 2, an interim real-time routing engine handles incoming prospect communication — an Inbound Reply Webhook Receiver ingests prospect email replies; Slack Routing (`#sales-replies`) posts interactive alert cards displaying prospect name, company domain, door count, full message thread history, and one-tap action buttons (`Reply in Thread`, `Book Meeting`, `Mark Opt-Out`); the Setter Queue Bridge (`#dial-tasks`) populates phone tasks with direct dial numbers and local timezone calculations.

#### 3.1.5 Inbound Conversion, Booking Engine & Show-Rate Cascade

```
BOOKING FLOW & SHOW-RATE CASCADE ARCHITECTURE

[Self-Serve Score Page / Email CTA] ──► [Google Calendar / Microsoft Graph Booking Form]
        │                                 (GoHighLevel fallback when client has no connectable calendar)
[Webhook Captures: Name, Work Email, Door Count]
        │
        ▼
[Creates `meeting_booked` Event in Database]
        │
        ▼
┌──────────────────────────────────────────────────────┐
│ 1. Instant Branded Confirmation Email + Calendar ICS │
│    (Email only — no SMS this year)                   │
└────────────────────────┬─────────────────────────────┘
        │
┌──────────────────────────────┴──────────────────────────────┐
▼                                                                ▼
[24 Hours Before Meeting]                          [30 Minutes Before Scheduled Meeting Time]
- Email Reminder with Prep Context                 - PRE-DEMO LEAD-IN EMAIL AUTO-DISPATCHED
- County-specific growth benchmarks                - Prospect's Owner Visibility Score PDF Attached
- One-click Reschedule Link                        - Agenda and meeting prep context
```

**Self-Serve Owner Score Landing Page:** High-converting public landing page at `audit.getblackink.com` where PMs can enter their corporate domain to request a certified Owner Visibility Score report. Automatically triggers background scoring workers, captures inbound lead details, and redirects high-intent prospects to the calendar booking interface. Embedded Meta and Google pixels build qualified retargeting audiences under strict wallet budgets.

**Show-Rate Reminder Chain:** Automated email confirmation sent immediately upon booking; 24-hour reminder email highlighting agenda and county-specific growth benchmarks; 30-minute pre-demo lead-in email attaching the Owner Visibility Score PDF. No SMS steps this year.

**No-Show & Reschedule Handler:** If a prospect fails to attend within 10 minutes of scheduled start, rep triggers Mark No-Show in Slack. Automatically pauses outreach sequences and enqueues a multi-channel recovery flow offering friction-free calendar re-booking via email.

**Response SLA:** Within 30 minutes during business hours; next business morning after hours.

#### 3.1.6 Rent Analysis Bot

**Rent Analysis Bot: Deferred to Q1. No RentCast or CoreLogic API contract exists. Define only a disabled adapter interface. Do not start a trial on the client's behalf. The pre-demo lead-in email must not advertise a rent bot phone number.**

#### 3.1.7 Demo Sandbox & Pipeline Metrics Engine

**Permanent Demo Friday Sandbox Client:** Formally designated, permanent test client environment populated with realistic operational data (Hillsborough/Pinellas county companies, realistic PM company names). Pulls up live Looker dashboards, active mock campaigns, and sample performance evidence packets on demand during sales calls.

**Pre-Demo Lead-In Automation:** Fires automatically exactly 30 minutes before any scheduled sales demonstration. Emails the prospect their Owner Visibility Score & Revenue Loss Report and confirms meeting agenda. Does NOT mention any SMS demo, rent bot, or phone number.

**60-Second Post-Meeting Form:** Closers complete a Slack modal following every completed meeting, capturing Meeting Attendance Status, Target PM Software, Estimated Door Count, Stated Objections, and Next Action.

**Pipeline Reporting & Slack Metrics Digest:** Real-time Looker Studio dashboards connected to PostgreSQL read-replicas, with an automated daily morning Slack digest posted to `#blackink-command` summarizing Owner Visibility Scores generated, county rank reports delivered, cold emails dispatched, open/click rates, and appointments booked.

#### 3.1.8 Week 1 Milestone Definition of Done

The Sprint 1 / Marketing Live milestone is officially cleared on September 11, 2026:

1. **Live Outbound Dispatch:** Demonstrate automated multi-touch email draft queued in `#blackink-setter` for human approval; verified email dispatching from warmed Google Workspace/Outlook inboxes across dedicated domains with verified SPF/DKIM/DMARC after Approve is clicked.
2. **Owner Visibility Score Report:** Generate a public-observable Owner Visibility Score for a target PM firm using the 10-category public scoring rubric; verify the PDF report displays score, county rank, data coverage percentage, and assumptions; confirm no pretext inquiry in any code path.
3. **Interactive Booking Flow:** Complete a live meeting booking through Google Calendar / Microsoft Graph integration; verify creation of the `meeting_booked` event in PostgreSQL; confirm immediate delivery of email confirmation with ICS attachment (no SMS).
4. **Lead-In Automation:** Trigger the 30-minute pre-demo lead-in email; verify Owner Visibility Score PDF is attached; verify no rent bot reference in email body.
5. **Interactive Slack Hub:** Execute approval, revision, and snooze actions inside `#blackink-setter` and `#sales-replies`, verifying that payload-bound hash security blocks altered payloads.
6. **Zero Cold SMS Outbound:** Execute the automated CI/CD compliance suite, demonstrating that cold outbound SMS is hard-blocked at the gate and rejected by runtime linters.

---

### Week 2 (Sept 14 – Sept 18, 2026): Sprint 2A — Settlement, Triage Agent & Founding Client Pilot

**Milestone Standard:** September 16–18 Founding Client Pilot Ready (Automated Inbound Reply Classification, Zero-Deposit Card Authorization & Settlement Rails, Tenant-Isolated Deliverability, and Hand-Assisted Tenant Deployment)

```
WEEK 2 COMPLETE CORE FULFILLMENT & SETTLEMENT PIPELINE

[Inbound Replies / Webhooks / Portal Leads]
        │
        ▼
┌──────────────────────────────────────┐
│ Reply Triage Agent (Classification)  │ ──► [Intent Taxonomy: 10 Classes]
└──────────────────┬───────────────────┘
        │
┌────────────────────────┼────────────────────────┐
▼                          ▼                          ▼
[High-Intent Lead]  [Objection / Question]  [Unsubscribe / Opt-Out]
- Setter Context Card - Auto-KB Response      - Deterministic DNC Suppression
- SLA Timers (15/60m) - Thread Routing         - Global Opt-Out Sync
- Booking Bridge      - Human Review Queue     - Sequence Immediate Halt
        │
        ▼
[Completed Appointment / Signed Agreement]
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Deterministic Settlement Engine & Stripe Rails                                        │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Zero-Deposit Card Auth + ACH Mandate Storage                                       │
│ 2. Nightly PM Read-Only Verification Sync (Syncs Doors/Agreements)                    │
│ 3. 50/50 Settlement Trigger: 50% Charged at Signature, 50% Charged at Day 60          │
│ 4. Automated 60-Day Clawback Monitor (Voids Back-Half if Churned)                     │
│ 5. Dynamic Evidence Packet PDF Auto-Attached to Every Invoice                         │
└──────────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
[Founding Client Pilot Environment: September 16–18 Live Deployment]
```

#### 3.2.1 Reply Triage Agent (1.4-Triage): Inbound Intent Classification & Routing

| Intent Class | Description                     | Automated Action                                                                      | Routing                               |
| ------------ | ------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------- |
| HOT_LEAD     | Explicit buying interest        | Halts cold sequence; generates 1-screen context card                                  | #blackink-setter & alert to rep       |
| QUESTION     | Informational queries           | Evaluates KB; drafts auto-response if confidence ≥90%, else queues for human review  | Thread in#sales-replies               |
| OBJECTION    | Pushback on timing, pricing     | Pulls objection handling playbook; equips Setter Copilot                              | Context card in#blackink-setter       |
| LATER        | Timing delay signal             | Ingests date into Reactivation memory; pauses campaign until target date              | Reactivation queue                    |
| NURTURE      | Mild interest                   | Transitions to low-frequency monthly educational nurture sequence                     | Nurture campaign stream               |
| UNSUBSCRIBE  | Removal requests                | Deterministic opt-out; writes`is_opted_out=TRUE` across entity and domain           | Compliance ledger                     |
| COMPLAINT    | Aggressive responses            | Immediately halts sequence; suppresses apex domain globally                           | #blackink-qa                          |
| LEGAL_GRIEF  | Legal threats, TCPA/CAN-SPAM    | Hard circuit-breaker trip; freezes all contacts; alerts executive Slack               | #blackink-command (P0)                |
| WHALE_OWNER  | Large portfolio (≥50 units)    | Enforces VIP routing; triggers instant alert to closer; locks high-priority SLA timer | Direct closer alert +#blackink-setter |
| PARTNER      | Realtor/vendor/lender inquiries | Routes to Referral Agent; categorizes partner type                                    | #client-growth (Referral Desk)        |

```sql
-- Schema: src/db/migrations/002_triage_routing.sql
CREATE TABLE inbound_messages (
  message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  contact_id UUID REFERENCES contacts(contact_id),
  channel VARCHAR(20) NOT NULL,
  raw_payload TEXT NOT NULL,
  cleaned_body TEXT NOT NULL,
  detected_intent VARCHAR(50) NOT NULL,
  confidence_score NUMERIC(5,2) NOT NULL,
  requires_human_review BOOLEAN DEFAULT FALSE,
  sla_due_at TIMESTAMP WITH TIME ZONE,
  status VARCHAR(50) DEFAULT 'RECEIVED',
  received_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE knowledge_base_entries (
  entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  topic VARCHAR(100) NOT NULL,
  trigger_patterns TEXT[] NOT NULL,
  approved_response_template TEXT NOT NULL,
  min_confidence_threshold NUMERIC(5,2) DEFAULT 0.90,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

**SLA Escalation Timers:** 15 minutes: unclaimed hot leads generate a second high-priority ping in Slack; 60 minutes: escalates to executive mobile notification; 240 minutes: automatically reallocates lead to the backup closer queue.

#### 3.2.2 Stripe Card Authorization & Settlement Rails (1.7-Stripe)

Blackink operates on a verified success-only economic model. Clients pay zero upfront fees, zero onboarding retainers, and zero monthly minimums prior to verified results (for appointment-based offers).

```
ZERO-DEPOSIT ONBOARDING & DETERMINISTIC SETTLEMENT PIPELINE

[Client Signs Agreement] ──► [Onboarding Flow Captures Stripe Card Auth + ACH Mandate Storage]
        │
[ZERO DOLLARS CHARGED UPFRONT]
        │
        ▼
[Campaigns Live] ──► [Nightly PMS Sync Confirms New Signed Management Agreement: `door_signed`]
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ 50/50 Billing Split Engine:                                                           │
│ 1. Triggers First 50% Charge via ACH (Card Backup)                                   │
│ 2. Compiles Dynamic Evidence Packet PDF                                               │
│ 3. Schedules 60-Day Clawback Verification Job in PostgreSQL                           │
└──────────────────────────────┬───────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ At Day 60: Automated PMS Verification Check Runs                                      │
├──────────────────────────────┬───────────────────────────────────────────────────────┤
│ [Agreement Active in PMS]    │ [Agreement Cancelled <60d]                             │
│ ──► Charge Remaining 50%     │ ──► Auto-Void Back-Half 50%                           │
│ ──► Email Receipt + PDF      │ ──► Log Clawback Event to DB                          │
└──────────────────────────────┴───────────────────────────────────────────────────────┘
```

**A. Card Authorization & ACH Mandate Capture:** During onboarding, the client submits card and bank details through a secure Stripe Elements modal. A temporary $1 authorization hold verifies card validity. ACH Direct Debit mandate established as primary billing rail; card retained as backup.

**B. 50/50 Settlement Split Mechanics:** Installment 1 (50% at Signature) charged immediately upon nightly verification; Installment 2 (50% at Day 60) placed into an automated scheduling queue.

**C. Automated 60-Day Clawback Trigger:** If a newly signed property is terminated within 60 days, the scheduled second 50% installment is automatically voided. A `settlement_clawback_executed` event is logged.

**D. Dynamic Evidence Packet Compiler:** Every charge automatically compiles a PDF Evidence Packet: Section 1 (Source & Outreach Lineage), Section 2 (Engagement & Booking Record), Section 3 (Meeting & Qualification Verification), Section 4 (PMS Contract Verification).

```sql
-- Schema: src/db/migrations/003_settlement_ledger.sql
CREATE TABLE settlement_transactions (
  transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL,
  owner_id UUID NOT NULL,
  property_id UUID NOT NULL,
  door_count INTEGER NOT NULL DEFAULT 1,
  total_bounty_cents BIGINT NOT NULL,
  installment_1_cents BIGINT NOT NULL,
  installment_2_cents BIGINT NOT NULL,
  installment_1_status VARCHAR(50) DEFAULT 'PENDING',
  installment_2_status VARCHAR(50) DEFAULT 'SCHEDULED',
  installment_1_charged_at TIMESTAMP WITH TIME ZONE,
  installment_2_scheduled_for TIMESTAMP WITH TIME ZONE,
  installment_2_charged_at TIMESTAMP WITH TIME ZONE,
  evidence_packet_url TEXT NOT NULL,
  stripe_invoice_id VARCHAR(100),
  is_clawed_back BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

#### 3.2.3 Tenant-Isolated Sending Reputations

```
TENANT-ISOLATED SENDING POOL ARCHITECTURE (20 DOMAINS / 40 MAILBOXES)

┌──────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┐
│ Blackink Internal Outbound (5 Domains / 10 Mailboxes) │ Client Outbound Dedicated Pools (15 Domains / 30 Mailboxes)│
├──────────────────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
│ - growth-getblackink.com (2 Mailboxes)               │ - Client 1 Dedicated Pool: 3 Domains (6 Mailboxes)         │
│ - connect-getblackink.com (2 Mailboxes)              │ - Client 2 Dedicated Pool: 3 Domains (6 Mailboxes)         │
│ - audit-getblackink.com (2 Mailboxes)                │ - Client 3 Dedicated Pool: 3 Domains (6 Mailboxes)         │
│ - pm-getblackink.com (2 Mailboxes)                   │ - Client 4 Dedicated Pool: 3 Domains (6 Mailboxes)         │
│ - scale-getblackink.com (2 Mailboxes)                │ - Client 5 Dedicated Pool: 3 Domains (6 Mailboxes)         │
└──────────────────────────────────────────────────────┴───────────────────────────────────────────────────────────┘
```

**Per-Mailbox Pacing:** Strict daily ceiling of 30–50 cold emails per mailbox/day with automated rotation across the client's 6 assigned mailboxes.

**Deliverability Sentinel & Auto-Quarantine:** If a domain records bounce rate >3% or spam complaint rate >0.08% within a rolling 48-hour window, the Sentinel trips: pauses outbound dispatch, replaces the degraded domain with a pre-warmed reserve domain, and posts an alert to `#blackink-qa`.

#### 3.2.4 Client Core Revenue Recipes Initialization

**A. The Win-Back Recipe (1.8-Rec)** — Ingests the client's historical dead leads, lost owners, and cancelled management agreements. Runs automated skip-tracing and DNC/suppression screening, then dispatches a 3-touch hyper-personalized re-engagement sequence from the client's own domain: Touch 1 (Market Shift Angle), Touch 2 (Ancillary Value), Touch 3 (Direct Check-in). Email only — no SMS, no AI voice calls.

**B. The Speed-to-Lead Recipe (1.9-Rec)** — Captures incoming owner inquiries from the client's website, listing portals, and paid campaigns, delivering responses within 30 minutes during business hours (next business morning after hours) via email with calendar booking link.

```
SPEED-TO-LEAD FLOW (DUAL INGESTION PATH)

┌───────────────────────────────────────┐  ┌────────────────────────────────────────┐
│ Path A: Direct Webhook Source          │  │ Path B: Unintegrated Email Notification │
│ (Website Form, Paid Landing Page)      │  │ (Zillow, Trulia, HotPads, MLS Forms)    │
└──────────────────┬────────────────────┘  └───────────────────┬────────────────────┘
        │ Webhook Ingest (<2s)                          │ Inbound Parse Hook (<5s)
        ▼                                                ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Speed-to-Lead Orchestrator (Validates Phone/Email & Checks Non-Poach Gate)        │
└──────────────────────────────────────────────────────┬───────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│ Automated Response Engine (Within 30 min business hours / next morning):          │
│ 1. Dispatches Personalized Confirmation Email with Real-Time Booking Calendar Link │
│ 2. Fires High-Priority Alert to Closer Queue in Slack #blackink-setter             │
│ (No SMS this year — email only)                                                    │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Email-Parsing Fallback:** For legacy listing sources, client inquiry notification emails route to a dedicated tenant parse address (`leads@{client-subdomain}.getblackink.com`), extracting prospect name, phone, address, and inquiry text via regex.

#### 3.2.5 Founding Client Pilot Deployment (September 16–18 Milestone)

| Stage                   | Operational Procedure                                                                                     | Acceptance Standard                                                   |
| ----------------------- | --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| 1. Tenant Cloning       | Clone Golden Client schema; inject tenant identifiers; provision dedicated 3-domain sending cluster.      | Isolated tenant workspace live with zero credential leakage.          |
| 2. Historical Ingest    | Ingest 500+ client dead leads via CSV import; execute automated DNC scrub.                                | Data normalized, deduped, suppression flags verified.                 |
| 3. Compliance Guard     | Execute cross-client non-poach check; verify opt-out tables; validate county-specific eligibility.        | Zero overlap with existing books; email-first waterfall holds.        |
| 4. Campaign Launch      | Arm Win-Back and Speed-to-Lead recipes; dispatch initial batch from client-dedicated mailboxes.           | Live outbound emails delivering; tracking webhooks active.            |
| 5. Triage & Routing     | Ingest live prospect replies; execute automated intent classification; post context cards to Slack.       | Replies classified correctly; hot leads routed to setter <60 seconds. |
| 6. Booking & Dashboards | Complete live meeting booking; verify calendar ICS; update Client Wins Dashboard with real event metrics. | Looker dashboard reflects live appointments and pipeline.             |

**Hands-On Engineering Support:** Engineering assistance is explicitly permitted during the September 16–18 pilot. Clients #1–2 are intentionally managed with hands-on technical guidance to observe friction points and harden the operational runbook before enforcing the zero-code standard on September 30.

#### 3.2.6 Week 2 Acceptance Criteria & Definition of Done

1. **Automated Reply Classification:** Process a test batch of 50 multi-channel replies across all 10 intent classes; verify ≥80% classification accuracy and correct routing to Slack with context cards.
2. **Deterministic Settlement Execution:** Execute a test outcome transaction in Stripe; verify zero dollars charged upfront, successful ACH mandate storage, Evidence Packet PDF generated, and 60-day clawback verification job created.
3. **Tenant-Isolated Sending Verification:** Demonstrate test campaigns for Tenant A execute exclusively through Tenant A's dedicated domain cluster.
4. **Speed-to-Lead Execution:** Trigger a test lead via webhook and email-parsing address; verify email response and closer Slack notification within 30 minutes business hours.
5. **Operational Pilot Tenant:** Live founding client tenant executing active win-back sequences, routing replies into Slack, and displaying real-time metrics in the Client Wins Dashboard.

---

### Week 3 (Sept 21 – Sept 25, 2026): Sprint 2B — 15-State Portal, Cloner Runbook, Demo Weapons & Preflight

**Milestone Standard:** Sprint 2B Production Readiness (Full 15-State Client Onboarding Portal, Client Cloner Runbook Path B, Phase 2A Retention Weapons, 72-Hour Launch Preflight Engine, and Operational Control Surfaces)

```
WEEK 3 COMPLETE ONBOARDING, WEAPONS & GOVERNANCE PIPELINE

[Signed Client Agreement / Contract Closed]
        │
[Automatic Link Generation / Signed URL]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 15-State Client Checklist Portal (`ADD-9-FULL`)                                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ States 1–7: Core Setup (Agreement, Auth, PM Profile, County Territory, Calendar Slots, Offer Approved)                  │
│ States 8–11: Holistic Value Intake (Historical Dead Leads, Document Upload, Partner Menu, Growth Elections)             │
│ States 12–15: Technical Launch (Isolated Sending, Lead Routing, Live-Fire Test, Client Preflight Approval)              │
└─────────────────────────────────────────┬────────────────────────────────────────────────────────────────────────────┘
┌──────────────────────┴──────────────────────┐
▼                                                ▼
┌────────────────────────────────────────────────────────┐  ┌───────────────────────────────────────────────────────────┐
│ Path B Client Cloner Runbook (`ADD-1-B`)                 │  │ Phase 2A Demo & Retention Weapons                            │
├────────────────────────────────────────────────────────┤  ├───────────────────────────────────────────────────────────┤
│ 1. Clone Golden Client Database & Schema                 │  │ 1. Churn Tripwire: Listing/Deed/Homestead Book Monitor       │
│ 2. Inject Tenant Profile & PM Software Credentials       │  │ 2. Owner/Portfolio Feed: Multi-LLC Door Aggregation           │
│ 3. Assign 3-Domain / 6-Mailbox Isolated Cluster          │  │ (Rent Analysis Bot: Deferred to Q1 — disabled adapter only)  │
│ 4. Auto-Generate First-14-Days Growth Plan               │  └─────────────────────────────┬─────────────────────────────┘
└──────────────────────────┬─────────────────────────────┘                                    │
        └──────────────────────────────┬──────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 72-Hour Preflight Validator (`PREFLIGHT`) ──► Asserts All 15 Dependencies Green ──► ARMS CAMPAIGNS                     │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Client View: Client Wins Dashboard (`DASH-WINS`)           │ Internal View: Internal Client Control Center (`OPS-CENTER`) │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 3.3.1 The 15-State Client Onboarding Portal (ADD-9-FULL)

```
THE 15-STATE ONBOARDING & HOLISTIC ASSESSMENT FLOW

[1. Agreement Signed] ──► [2. Payment Auth (Card + ACH)] ──► [3. Contacts Entered] ──► [4. PM Profile]
        │
[8. Historical Ingest] ◄── [7. Offer / CTA Approved] ◄── [6. Calendar (25 Slots)] ◄── [5. County Territory Set]
        │
        ├──► [9. Complete Document Upload ──► Auto-Generates Instant Revenue-Stack Audit Map]
        ├──► [10. Partner Menu Reviewed ──► Checkbox Enrollments Logged for Ancillary Lines]
        └──► [11. Growth & Value Elections ──► Captures New Fees, Streams & Target Services]
        │
[15. Preflight Green / Live] ◄── [14. Live-Fire Test Passed] ◄── [13. Routing Confirmed] ◄── [12. Compliance / Domains]
```

**State Specifications:**

- **State 1 — Agreement Signed:** Contract execution verified via webhook; durable acceptance record stored including agreement version, accepted_at, accepted_by_email, IP, and hash of exact text shown.
- **State 2 — Payment Authorization Complete:** Zero-deposit payment setup via Stripe Elements; ACH mandate captured; card authorization held.
- **State 3 — Primary Client Contacts Entered:** Broker/Owner and Operations/Office Lead contact details validated.
- **State 4 — Property Management Profile Completed:** Populates `pm_profile` schema (specialty tags, asset classes, accepted property types, languages, historical performance metrics).
- **State 5 — County Territory Established:** Defines the contracted geographic territory via county and contracted polygon/ZIP arrays. Sets client acceptance criteria and links the non-poach suppression perimeter. No metro commercial layer.
- **State 6 — Calendar Connected:** Google Calendar or Microsoft Graph OAuth; GoHighLevel fallback when client has neither. Minimum 25 open meeting slots across the initial 60-day window verified.
- **State 7 — Client Offer & CTA Approved:** Confirms standard messaging hooks, switching incentives, and target customer profiles. No agent-written fee promises.
- **State 8 — Full Historical Data Ingest (Win-Back Goldmine):** Portal upload of historical CRM exports, past owner lists, cancelled management agreements, and dead leads. Current owner file for suppression is mandatory; historical dead-book data seeds Win-Back recipe.
- **State 9 — Complete Document Upload & Instant Revenue Audit:** Client uploads management agreement, fee schedule, and operational addenda, triggering the ADD-8-Lite Fee-Stack pipeline to generate an immediate, branded Revenue-Stack Audit.
- **State 10 — Partner Menu Selections:** Interactive interface presenting pre-negotiated partner lines (pet screening, resident benefits, utility concierge, filter delivery, deposit alternatives, maintenance markup policies, eviction protection, Ryse rent advance). Mark unavailable services as pending, not live.
- **State 11 — Growth & Value Elections:** Structured intake capturing strategic growth goals beyond door count.
- **State 12 — Sending Identity & Compliance Approved:** Assigns dedicated sending domains; generates bidirectional non-poach suppression lists. Configures delegated subdomain, Reply-To to client's own address, and BCC on every send.
- **State 13 — Campaign & Lead Routing Confirmed:** Configures inbound lead-capture webhooks and dedicated email-parsing address (`leads@{client-subdomain}.getblackink.com`).
- **State 14 — Live-Fire Test Passed:** Automated end-to-end test verifying that an injected synthetic lead triggers a notification within 30 minutes (business hours), schedules a calendar event, and alerts the closer queue in Slack. Email only — no SMS.
- **State 15 — Preflight Green & Launch Approved:** Preflight validation engine evaluates all prerequisites; client clicks "Approve Launch," arming live outbound campaigns.

```sql
-- Schema: src/db/migrations/004_onboarding_states.sql
CREATE TABLE client_onboarding_states (
  client_id UUID PRIMARY KEY REFERENCES companies(company_id) ON DELETE CASCADE,
  state_1_agreement_signed BOOLEAN DEFAULT FALSE,
  state_2_payment_authorized BOOLEAN DEFAULT FALSE,
  state_3_contacts_entered BOOLEAN DEFAULT FALSE,
  state_4_pm_profile_complete BOOLEAN DEFAULT FALSE,
  state_5_territory_established BOOLEAN DEFAULT FALSE,
  state_6_calendar_connected BOOLEAN DEFAULT FALSE,
  state_7_offer_approved BOOLEAN DEFAULT FALSE,
  state_8_history_imported BOOLEAN DEFAULT FALSE,
  state_9_documents_uploaded BOOLEAN DEFAULT FALSE,
  state_10_partner_menu_reviewed BOOLEAN DEFAULT FALSE,
  state_11_growth_elections_captured BOOLEAN DEFAULT FALSE,
  state_12_compliance_configured BOOLEAN DEFAULT FALSE,
  state_13_routing_confirmed BOOLEAN DEFAULT FALSE,
  state_14_live_fire_passed BOOLEAN DEFAULT FALSE,
  state_15_launch_approved BOOLEAN DEFAULT FALSE,
  current_state_number INTEGER DEFAULT 1,
  portal_access_token VARCHAR(255) UNIQUE NOT NULL,
  portal_token_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  completed_at TIMESTAMP WITH TIME ZONE,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE client_growth_elections (
  election_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  category VARCHAR(100) NOT NULL,
  item_name VARCHAR(255) NOT NULL,
  selection_status VARCHAR(20) NOT NULL,
  modeled_annual_value_cents BIGINT DEFAULT 0,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

**Auto-Chase Suppression Logic:** As the client completes each state, an event is logged. The Launch Agent detects completion events in real time and cancels corresponding chase notifications. Abandonment states (paid-but-calendar-blocked, paid-but-file-missing) trigger nudges at 24 hours, 72 hours, and 7 days. No second month is billed while activation is blocked on Blackink's side.

#### 3.3.2 Client Cloner & Launch OS (Path B Runbook Architecture)

```
PATH B CLIENT CLONING & PROVISIONING ARCHITECTURE

┌──────────────────────────────────────────────┐
│ Golden Client Master Template                 │
│ (Isolated Sandbox Environment Schema)         │
└──────────────────────┬───────────────────────┘
        ▼
┌──────────────────────────────────────────────┐
│ Step 1: Database & Schema Provisioning        │
│ - Execute `clone_tenant_environment.sh`       │
│ - Provision isolated tenant row in Postgres   │
│ - Enforce tenant isolation via `client_id`    │
└──────────────────────┬───────────────────────┘
        ▼
┌──────────────────────────────────────────────┐
│ Step 2: Infrastructure & Deliverability       │
│ - Assign 3 Dedicated Domains (6 Mailboxes)    │
│ - Connect Instantly Sub-Workspace via API     │
│ - Verify SPF/DKIM/DMARC & Warmup Status       │
│ - Provision Dedicated Inbound Telnyx Number   │
└──────────────────────┬───────────────────────┘
        ▼
┌──────────────────────────────────────────────┐
│ Step 3: Non-Poach & Compliance Provisioning   │
│ - Ingest Client Book into Suppression Engine  │
│ - Generate Bidirectional Cross-Client Gate    │
│ - Apply Target County Polygon Boundary        │
└──────────────────────┬───────────────────────┘
        ▼
┌──────────────────────────────────────────────┐
│ Step 4: First-14-Days Plan Auto-Generation    │
│ - Parse States 8–11 Intake Payloads           │
│ - Assemble Engine Activation Schedule         │
│ - Render Plan into Client Wins Dashboard      │
└──────────────────────────────────────────────┘
```

1. **Database & Tenant Keying:** Operator runs `clone_tenant_environment.sh`. Provisions new tenant profile; binds cryptographic API tokens; applies row-level isolation rules.
2. **Dedicated Deliverability Cluster Allocation:** Allocates dedicated cluster of 3 sending domains and 6 Google Workspace mailboxes; provisions a dedicated Telnyx local phone number mapped to the client's target county.
3. **Suppression & Compliance Initializer:** Ingests client's current owner roster into non-poach database, establishing cross-suppression rules bidirectionally.
4. **First-14-Days Plan Generator:** Reviews client historical dead-lead count, target county, and fee schedule to generate operational roadmap — Days 1–3: Win-Back activation + Speed-to-Lead routing; Days 4–7: Churn Tripwire activation + initial outbound launch; Days 8–14: Ancillary Partner Menu integration + first weekly performance review.
5. **Rollback & Circuit Breaker Engine:** Documented rollback runbook (`rollback_tenant_provisioning.sh`) revokes API keys, freezes sending queues, decouples suppression joins, and restores the database to its pre-clone state.

#### 3.3.3 Phase 2A Retention Weapons

```
CHURN TRIPWIRE RETENTION MONITORING ENGINE (`2A-TRIPWIRE`)

[Active Client PM Book Ingested & Synced]
        ▼
┌───────────────────────────────────────┐
│ Nightly Public Records & Market Sweep  │
└───────────────────┬───────────────────┘
┌───────────────────────────────────────┼───────────────────────────────────────┐
▼                                        ▼                                        ▼
[MLS / Public Listing Detector]    [Deed Transfer & Sale Monitor]    [County Tax Record Auditor]
- Active For-Sale Listings          - County Clerk Deed Recordings    - Homestead Exemption Drops
- FSBO Listings                     - Title Transfers                  - Mailing Address Divergence
- Price Cuts & Status Changes       - Pre-Foreclosure / Lis Pendens    - Out-of-State Relocations
└───────────────────────────────────────┼───────────────────────────────────────┘
        ▼
┌───────────────────────────────────────────────────────────────┐
│ Correlation Engine (Matches Properties to Client Owner Book)    │
└───────────────────────────────┬───────────────────────────────┘
        ▼
┌───────────────────────────────────────────────────────────────┐
│ 1. Generates Real-Time Retention Risk Alert in Slack            │
│ 2. Assembles Owner Save Dossier (Property, Signal, Next Step)   │
│ 3. Logs `door_saved` Opportunity to Events Ledger               │
└───────────────────────────────────────────────────────────────┘
```

**A. Churn Tripwire (2A-TRIPWIRE)** — Continuously cross-references every property address and owner entity in the client's active management database against daily county public records and market signals. Signal Detection Classes: MLS Listing Filings, Deed Transfers & Title Changes, Tax & Homestead Exemption Drops, Out-of-State Mailing Address Changes, Management Agreement Anniversary Flags.

**B. Owner & Portfolio Ingestion Engine (2A-FEED-MIN)** — LLC Entity Resolution ingestion pipeline processes purchased portfolio datasets, linking business entities to parent LLCs and individual managing members. Door Aggregation calculates total residential door counts per owner across Florida counties.

**Rent Analysis Bot:** Deferred to Q1. Disabled adapter interface only. No RentCast or CoreLogic API calls.

#### 3.3.4 72-Hour Launch Preflight Engine & Health Gates

```sql
-- Schema: src/db/migrations/005_preflight_validator.sql
CREATE TABLE client_preflight_checks (
  check_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID UNIQUE NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  check_agreement_executed BOOLEAN DEFAULT FALSE,
  check_stripe_auth_verified BOOLEAN DEFAULT FALSE,
  check_pm_profile_valid BOOLEAN DEFAULT FALSE,
  check_territory_non_empty BOOLEAN DEFAULT FALSE,
  check_calendar_slots_count INTEGER DEFAULT 0,
  check_history_records_count INTEGER DEFAULT 0,
  check_documents_uploaded BOOLEAN DEFAULT FALSE,
  check_partner_menu_completed BOOLEAN DEFAULT FALSE,
  check_domains_spf_dkim_dmarc_valid BOOLEAN DEFAULT FALSE,
  check_non_poach_suppression_active BOOLEAN DEFAULT FALSE,
  check_live_fire_roundtrip_passed BOOLEAN DEFAULT FALSE,
  is_preflight_green BOOLEAN DEFAULT FALSE,
  last_evaluated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

| Gate ID | Prerequisite Validated                       | Acceptance Standard                                                               |
| ------- | -------------------------------------------- | --------------------------------------------------------------------------------- |
| GATE-01 | Stripe Authorization & ACH Mandate           | setup_intent status == 'succeeded'                                                |
| GATE-02 | PM Profile Schema Completeness               | All required fields populated                                                     |
| GATE-03 | County Territory Definition & Polygon Bounds | ≥1 valid county + polygon assigned                                               |
| GATE-04 | Calendar Meeting Availability                | ≥25 open slots in next 60 days                                                   |
| GATE-05 | Historical Data Ingestion                    | ≥50 historical records normalized                                                |
| GATE-06 | Document Ingestion & Audit Generation        | Fee schedule uploaded; audit compiled                                             |
| GATE-07 | Partner Menu Selections                      | All partner lines reviewed                                                        |
| GATE-08 | Dedicated Sending Domain Verification        | SPF, DKIM, DMARC 100% valid                                                       |
| GATE-09 | Bidirectional Non-Poach Suppression          | Client book cross-indexed in gate                                                 |
| GATE-10 | Live-Fire Roundtrip Verification             | Synthetic lead generates email response within 30 min business hours (email only) |

#### 3.3.5 Operational Dashboards & Control Surfaces

**A. Client Wins Dashboard (DASH-WINS):** Displays attended discovery appointments held, verified signed management agreements with door counts, total saved doors identified by Churn Tripwire, modeled and collected ancillary revenue, and downloadable Evidence Packet PDFs.

**B. Internal Client Control Center (OPS-CENTER):** Single administrative screen providing visibility across all client tenants — live status of 15 onboarding states, real-time deliverability health, pipeline volume metrics, and open exception queue.

#### 3.3.6 Week 3 Acceptance Criteria & Definition of Done

1. **Complete 15-State Onboarding Demonstration:** Walk through portal using a tokenized link; upload sample management agreements and dead leads; confirm completion events suppress chase notifications.
2. **Instant Revenue Audit Generation:** Upload sample fee schedule in State 9; verify branded Revenue-Stack Audit PDF is generated.
3. **Path B Cloner Execution:** Execute manual cloning runbook; verify database creation, tenant parameter injection, isolated domain assignment, and suppression indexing.
4. **Churn Tripwire Alert Verification:** Inject a synthetic deed transfer and MLS listing event matching a client property; verify system detects match, posts retention alert to Slack, and creates save opportunity in ledger.
5. **Preflight Deterministic Gating:** Demonstrate that Preflight engine blocks campaign arming when a prerequisite is missing, and unlocks only when all 10 gates evaluate green.
6. **Control Surfaces Operational:** Verify Client Wins Dashboard reflects real-time metrics and Internal Control Center accurately displays tenant health.

---

### Week 4 (Sept 28 – Sept 30, 2026): Sprint 3 — Verification Engine, Retention Suite, Expansion Registry & Zero-Code Acceptance

**Milestone Standard:** September 30 Blackink Business-in-a-Box Production Complete

```
WEEK 4 COMPLETE VERIFICATION, RETENTION & ACCEPTANCE ARCHITECTURE

┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ The 4-Rule Attended Appointment Verification Engine & Dispute Adjudication                                             │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Rule 1: Assessor Parcel ID Verified in Polygon        │ Rule 2: Live Attended ≥12 Mins (`proof_ref` Log)               │
│ Rule 3: Pre-Qualification Documented on Row           │ Rule 4: Matches Client Initialed ICP (Exhibit A)                │
│ ──► QA Watchdog Auto-Adjudicates Disputes (Duration + Transcript Ownership Language Check ──► Auto-Deny / Escalate)   │
└──────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────────┘
┌───────────────────────┴───────────────────────┐
▼                                                  ▼
┌────────────────────────────────────────────────────────┐  ┌───────────────────────────────────────────────────────────┐
│ Phase 3 Retention Suite                                  │  │ Expansion Engine & Compounding Sub-Engines                    │
├────────────────────────────────────────────────────────┤  ├───────────────────────────────────────────────────────────┤
│ 1. Retention Guard ($399/mo Book Churn Monitor)          │  │ 1. Offer Registry (`entitlement_offers` Table)                 │
│ 2. Rent Gap Report ($197/mo Under-Market Engine)         │  │ 2. One-Click In-App Activation (Flips Entitlement Row)         │
│ 3. Owner Report Card ($197/mo White-Label PDF)           │  │ 3. Close Detection (Forwarded PM Email Parser)                 │
│ 4. Anniv. Flags & Deed/Listing/Homestead Watch           │  │ 4. Dead-Book Engine, 4-Channel Referrals, Second Pass          │
└──────────────────────────┬─────────────────────────────┘  └─────────────────────────────┬─────────────────────────────┘
        └───────────────────────────────┬─────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Production Security Baseline (`SEC-BASE`) & Governance Ledgers                                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ SEPTEMBER 30 ACCEPTANCE TEST: BUSINESS-IN-A-BOX ZERO-CODE REPEATABILITY                                                │
│ Golden Client Run ──► Repeat on Tenant #2 via Written Runbook ──► ZERO Code Touched = PRODUCTION COMPLETE              │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 3.4.1 The 4-Rule Attended Appointment Verification Engine & Dispute Adjudication

Under Blackink's performance-based billing model, revenue is recognized on verified attended appointments. Every appointment is evaluated programmatically against a 4-rule qualification bar before an invoice is issued.

**Billable appointment must have ALL of the following:**

1. **Rule 1 (Assessor Parcel ID Verification):** The booking payload must resolve to a verified residential property within the client's contracted geographic territory; county tax assessor parcel ID (APID) validated against public property feeds.
2. **Rule 2 (Live Attendance Duration ≥12 Minutes):** Both the property owner and the client representative must remain on the calendar bridge for a minimum of 12 verified minutes, stored as `proof_ref`.
3. **Rule 3 (Documented Pre-Qualification):** The outcome row must contain the prospect's explicit pre-qualification statement (own or have legal management authority over ≥1 residential rental in contracted county; hold as investment; not already managed by client; ≤60 days remaining on existing agreement or terminable at will; stated consideration within 90 days; matches client's accepted property selections).
4. **Rule 4 (ICP Alignment per Exhibit A):** Unit count, property type, and asset class must fall within the client's contracted ICP criteria (residential single-family/small multifamily; not commercial, raw land, mobile home on leased land, or exclusively short-term rental).

**Failure Allocation:** If Rule 1, 3, or 4 fails, Blackink absorbs the cost and no charge is generated. A replacement credit is issued only if Rule 2 fails due to a legitimate, documented prospect no-show or early disconnect (1 free replacement per 4 billables, max 4/month).

```sql
-- Schema: src/db/migrations/006_verification_disputes.sql
CREATE TABLE appointment_outcomes (
  outcome_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  contact_id UUID NOT NULL REFERENCES contacts(contact_id),
  appointment_scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
  attended_duration_seconds INTEGER NOT NULL DEFAULT 0,
  proof_ref TEXT NOT NULL,
  parcel_id_verified VARCHAR(100) NOT NULL,
  pre_qualification_text TEXT NOT NULL,
  icp_criteria_passed BOOLEAN DEFAULT FALSE,
  is_billable BOOLEAN DEFAULT FALSE,
  billing_status VARCHAR(50) DEFAULT 'PENDING',
  dispute_state VARCHAR(50) DEFAULT 'NONE',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE dispute_adjudications (
  adjudication_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  outcome_id UUID UNIQUE NOT NULL REFERENCES appointment_outcomes(outcome_id) ON DELETE CASCADE,
  client_dispute_reason TEXT NOT NULL,
  qa_agent_duration_check BOOLEAN NOT NULL,
  qa_agent_transcript_keyword_match BOOLEAN NOT NULL,
  adjudication_verdict VARCHAR(50) NOT NULL, -- 'AUTO_DENY', 'AUTO_CREDIT', 'ESCALATE_TO_FOUNDER'
  verdict_explanation TEXT NOT NULL,
  adjudicated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

**Dispute Adjudication:** Clients have a 5-business-day window to flag an appointment outcome. QA Watchdog auto-adjudicates: duration verification checks `proof_ref` duration log; transcript evaluation checks ownership context language; deterministic verdict issued. Retention Floor Rule: if rolling 90-day signed-to-attended conversion ≥15%, no goodwill credits owed. If conversion drops below 8% for two consecutive months, either party may terminate without penalty.

#### 3.4.2 Phase 3 Retention Suite

```
PHASE 3 RETENTION SUITE ARCHITECTURE

[Client's Active Managed Doors Synchronized]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Retention Guard Engine ($399/month Subscription)                                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. MLS & FSBO Sale Listings       4. Out-of-State Tax Address Changes                                                   │
│ 2. County Deed Transfers          5. Agreement Anniversary Triggers (60 days prior)                                     │
│ 3. Homestead Exemption Filings                                                                                          │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Retention Guard ($399/month):** Nightly public records and market scans across every property in the client's database, flagging sell signals, price drops, and listing filings.

**Rent Gap Report ($197/month):** Ingests active lease rates and compares against real-time hyper-local market comps, compiling a rent-increase brief for units ≥10% under market.

**Owner Report Card ($197/month):** White-labeled quarterly property performance reports for each property owner.

#### 3.4.3 Expansion Engine, Offer Registry & Sub-Engines

```sql
-- Schema: src/db/migrations/007_expansion_registry.sql
CREATE TABLE entitlement_offers (
  offer_id VARCHAR(100) PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  price_cents BIGINT NOT NULL,
  billing_model VARCHAR(50) NOT NULL,
  stripe_price_id VARCHAR(100) NOT NULL,
  trigger_condition VARCHAR(100) NOT NULL,
  eligibility_predicate TEXT NOT NULL,
  cooldown_days INTEGER DEFAULT 30,
  monthly_cap INTEGER,
  is_enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE client_active_entitlements (
  entitlement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  offer_id VARCHAR(100) NOT NULL REFERENCES entitlement_offers(offer_id),
  status VARCHAR(50) DEFAULT 'ACTIVE',
  activated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMP WITH TIME ZONE
);
```

**Corrected Commercial Offer Matrix (September active):**

| Row Key                    | Display Name                | Price                | Model         | Notes                                                                         |
| -------------------------- | --------------------------- | -------------------- | ------------- | ----------------------------------------------------------------------------- |
| respond                    | Inbound Speed-to-Lead       | $249/month           | Subscription  | Self-serve. Human approval per send (early phase). 30-min SLA business hours. |
| respond_bundle             | Respond + Retention Guard   | $599/month           | Subscription  | Self-serve. Bundle discount.                                                  |
| retention_guard            | Client Book Churn Monitor   | $399/month           | Subscription  | Add-on to respond_bundle.                                                     |
| retention_guard_standalone | Retention Guard Standalone  | $499/month           | Subscription  | Without Respond.                                                              |
| appt_first                 | First Attended Meeting      | $49                  | Metered Event | Replaces $97; once per customer.                                              |
| appt_standard              | Attended Appointment (flat) | $99 flat             | Metered Event | Any door count. Replaces all door-band pricing. Passes 4-rule bar.            |
| growth_os_founding         | Growth OS (Founding)        | $897/month           | Subscription  | First 25 clients; human-sold.                                                 |
| growth_os_standard         | Growth OS (Standard)        | $997/month           | Subscription  | Human-sold.                                                                   |
| appt_deadbook              | Dead-Book Reactivated Appt  | $75                  | Metered Event | Sourced from client historical dead-book.                                     |
| extra_engine               | Extra Engine                | $97/month            | Subscription  | Add-on engine.                                                                |
| extra_office               | Extra Office                | $79/month            | Subscription  | Add-on office seat.                                                           |
| deadbook_engine            | Dead-Book Campaign Suite    | $99/month + $75/appt | Sub + Event   | Recurring activation + appointment fee.                                       |
| county_additional          | Additional County           | $397/month           | Subscription  | Adjacent eligible county.                                                     |
| rent_gap_report            | Under-Market Rent Report    | $197/month           | Subscription  | ≥5 units under market.                                                       |
| owner_report_card          | Quarterly Owner PDF Report  | $197/month           | Subscription  | White-label retention reports.                                                |

**Note:** Final 12-product Stripe manifest is pending — the above reconciliation table is not yet a complete approved Stripe activation. County seat SKUs (`seat_a_door_gen`, `seat_b_comp_intel`, `seat_both`, `seat_adjacent`) exist as disabled rows for September; do not create active Stripe seat SKUs this month.

**Sub-Engines:**

- **One-Click In-App Activation:** When a client approves an expansion recommendation, the system atomically flips the entitlement row and updates Stripe billing.
- **Close Detection Email Parser:** Ingests forwarded confirmation emails from client PM software; extracts owner and property data via regex; sets `signed=TRUE` on the corresponding outcome row.
- **Dead-Book Reactivation Engine:** Connects to client historical dead-lead lists; runs compliant outreach from the client's own domain; billed at $99/month plus $75 per attended appointment.
- **4-Channel Referral Credit Ledger:** Client-to-Client Referral ($250 credit at first paid month); New County Referral ($500 credit); Free-Tier Referral (1 free appointment credit); Vendor Introductions (reciprocal revenue-share in 4 tranches: $20/$10/$10/$10).
- **Second Pass & Unsold Routing:** Where an attended appointment does not convert within 90 days, the pre-qualified owner routes into the Second Pass pool ($75/appointment) with transferability consent verified.

#### 3.4.4 Production Security Baseline (SEC-BASE) & Governance Ledgers

**A. Cross-Tenant Leakage Test Suite:** Automated adversarial test suite executes nightly in CI/CD, injecting synthetic tenant records and simulating cross-tenant database lookups. Asserts database queries partition strictly by `client_id`.

**B. Automated Backups & Disaster Recovery:** Automated daily PostgreSQL snapshots with point-in-time recovery (PITR). Full database restore executed in an isolated staging environment (no production PII in staging). Nightly backups begin day one; tested restore before first paying client.

**C. Governance & Cost Ledgers:**

- **Consent & Channel Eligibility Ledger (`CONSENT-LEDGER`):** Immutable datastore per contact — exact consent capture timestamps, source list attribution, channel eligibility, opt-out expiration.
- **Per-Client Cost Ledger (`COST-LEDGER`):** Variable cost tracking per tenant (enrichment, Telnyx, Instantly mailboxes, AI tokens, paid spend). Net contribution margin calculated. Economics Governor Bands: Green (<$200/signed deal), Yellow ($200–$300), Orange ($300–$400), Red (>$400 → hard pause on paid channels).
- **Wallet-Capped Paid Growth:** Google Search and Meta retargeting under strict database wallet caps ($150–$300 initial risk budget per client).

#### 3.4.5 The September 30 Production Complete Acceptance Test

| Stage                        | Operational Procedure                                                                                          | Acceptance Standard                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 1. Golden Client Demo        | Run complete lifecycle: Clone → Onboard → Launch → Service → Measure → Bill with Evidence Packet          | Demonstrated live; all event records valid in DB                     |
| 2. Second Tenant Repeat      | Execute Path B Cloner Runbook on fresh test tenant; import dead leads, verify preflight, launch, and bill      | Completed by operator following written runbook with ZERO code edits |
| 3. Verification & Settlement | Inject attended appointment; verify 4-rule evaluation, Stripe payment intent, and dynamic Evidence PDF         | Settlement row created; ACH drafted; evidence PDF attached           |
| 4. Auto-Dispute Adjudication | Submit test dispute; verify QA Watchdog evaluates duration and transcript, auto-denying or crediting correctly | Verdict rendered programmatically in <30 seconds                     |
| 5. Retention Guard Alert     | Trigger synthetic deed transfer on client book; verify Retention Guard detects risk and logs save opportunity  | Alert rendered in Slack; door_saved logged to ledger                 |
| 6. Security & Restore QA     | Execute CI/CD cross-tenant leakage test suite; demonstrate successful database restore in staging              | All assertion tests pass green; zero data leakage verified           |

**FINAL VERDICT: PRODUCTION COMPLETE**

#### 3.4.6 Week 4 Acceptance Criteria & Final Definition of Done

1. **4-Rule Verification & Dispute Adjudication:** Automated qualification of attended appointment against all 4 rules; synthetic dispute renders deterministic QA Watchdog verdict.
2. **Phase 3 Retention Suite Live:** Retention Guard detects listing and deed changes; Rent Gap Report identifies under-market units; Owner Report Card compiles branded quarterly PDF.
3. **Expansion Offer Registry:** One-click in-app activation of add-on offer; atomic entitlement update and Stripe subscription change confirmed.
4. **Sub-Engines Operational:** Close-detection parser marks `signed=TRUE`; dead-book reactivation runs on client domains; 4-channel referral credits update ledger; Second Pass routes 90-day unclosed leads.
5. **Security Baseline & Live Restore:** Passing cross-tenant leakage tests; Consent and Cost Ledger records confirmed; successful database backup and live restore in staging demonstrated.
6. **Two-Tenant Zero-Code Repeatability:** Full business-in-a-box lifecycle on Golden Client followed by clean execution on second test tenant via written runbook without touching a single line of application code.

---

## 4. Commercial Offer & Database Row Matrix

All commercial terms exist strictly as database rows in the offer registry with Stripe Price IDs. No pricing logic is compiled into agents.

| Row Key                    | Display Name                       | Commercial Model                 | Trigger & Verification Rule                                                                                                                                                             |
| -------------------------- | ---------------------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| respond                    | Inbound Speed-to-Lead Tier         | $249/month Subscription          | Lead Agent answering owner inquiries within 30 min business hours; next business morning after hours. Human approval required for each send in early client phase. Self-serve eligible. |
| respond_bundle             | Respond + Retention Guard Bundle   | $599/month Subscription          | Self-serve checkout bundle.                                                                                                                                                             |
| retention_guard            | Client Book Churn Monitor (add-on) | $399/month Subscription          | Nightly sweep on client's own owners for listing/deed changes.                                                                                                                          |
| retention_guard_standalone | Retention Guard Standalone         | $499/month Subscription          | Without Respond.                                                                                                                                                                        |
| appt_first                 | First Attended Meeting Credit      | $49 Metered Event                | Once per customer; replaces $97. Passes 4-rule verification bar.                                                                                                                        |
| appt_standard              | Attended Appointment (flat rate)   | $99 flat Metered Event           | Any door count. Replaces all per-door-band pricing. Passes 4-rule verification bar.                                                                                                     |
| growth_os_founding         | Growth OS Founding                 | $897/month Subscription          | First 25 clients; human-sold; all county engines active.                                                                                                                                |
| growth_os_standard         | Growth OS Standard                 | $997/month Subscription          | Human-sold; standard rate after founding cohort.                                                                                                                                        |
| appt_deadbook              | Dead-Book Reactivated Appointment  | $75 Metered Event                | Sourced from client historical dead-book ingest. Passes 4-rule bar.                                                                                                                     |
| extra_engine               | Extra Engine Add-On                | $97/month Subscription           | Additional engine activation.                                                                                                                                                           |
| extra_office               | Extra Office Seat                  | $79/month Subscription           | Additional office user.                                                                                                                                                                 |
| deadbook_engine            | Dead-Book Campaign Suite           | $99/month + $75/appt Sub + Event | Ongoing dead-book monitoring on client domains.                                                                                                                                         |
| county_additional          | Additional County                  | $397/month Subscription          | ≥10 doors in adjacent eligible county.                                                                                                                                                 |
| rent_gap_report            | Under-Market Rent Report           | $197/month Subscription          | Below-market engine; ≥5 under-rented units.                                                                                                                                            |
| owner_report_card          | Quarterly Owner PDF Report         | $197/month Subscription          | Client active 60+ days; white-label retention reports.                                                                                                                                  |

**Note:** County seat products (`seat_a_door_gen`, `seat_b_comp_intel`, `seat_both`, `seat_adjacent`) are held as disabled rows for post-September; do not activate Stripe seat SKUs this month.

---

## 5. Blackink Governed Nine-Agent Workforce Architecture

### 5.1 The Agent Operating System (AOS) & Shared Core Architecture

```
BLACKINK AGENT OPERATING SYSTEM (AOS) ARCHITECTURAL TOPOLOGY

┌──────────────────────────────┐
│      Slack Agent Hub          │
│  (@Blackink Central Router)   │
└──────────────┬───────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ SHARED AGENT CORE (CHASSIS)                                                                                             │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ • Tenant Isolation (`client_id` Scoping)         • Payload-Bound Cryptographic Hash Approvals (`SHA-256`)              │
│ • Distributed Leases & Cross-Agent Locking       • Centralized Memory Spine & Context Store                            │
│ • Idempotency Keys & Retry State Machines        • Observability, Heartbeats & Circuit Breakers                        │
└────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────┘
┌───────────────────────────────────────────┼───────────────────────────────────────────┐
▼                                              ▼                                              ▼
┌─────────────────────────────────┐  ┌─────────────────────────────────┐  ┌─────────────────────────────────┐
│ FRONT-OFFICE AGENTS               │  │ MID-OFFICE AGENTS                 │  │ BACK-OFFICE & GOVERNANCE AGENTS   │
├─────────────────────────────────┤  ├─────────────────────────────────┤  ├─────────────────────────────────┤
│ 1. Prospecting Agent (Hunter)     │  │ 4. Lead Agent (Reply Triage)      │  │ 7. Economics & Growth (Vera)      │
│ 2. Campaign Agent (Cora/Relay)    │  │ 5. Reactivation & Nurture Agent   │  │ 8. QA / Watchdog Agent            │
│ 3. Setter Copilot                 │  │ 6. Referral Agent                 │  │ 9. Launch Agent (Cloner/Launch)   │
└─────────────────────────────────┘  └─────────────────────────────────┘  └─────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC PLATFORM GATES (UNBYPASSABLE HARD STOPS — NO LLM ACCESS)                                                 │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ • Non-Poach Cross-Client Suppression Engine       • National/State DNC Linter & Quiet-Hours Filter                     │
│ • Stripe Billing & Settlement State Machine       • Consent & Channel Eligibility Ledger                               │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Subsystem                  | Technical Implementation & Invariant Rule                                                                                                                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Tenant Isolation           | Every database row, Redis key, cache entry, vector embedding, and audit record is strictly partitioned by`client_id`. CI/CD executes adversarial cross-tenant leakage tests nightly.                                                                                                                       |
| Payload-Bound Approvals    | Every Slack approval button (Approve, Revise, Reject, Snooze, Skip, Mark Done) is cryptographically bound to SHA-256 hash of exact message body, recipient ID, and configuration state. An action cannot execute under an outdated click if the underlying payload has changed. Cards expire after 24 hours. |
| Cross-Agent Object Locks   | Distributed Redis leases prevent race conditions. If Campaign Agent holds an active lock on an opportunity, Reactivation Agent cannot initiate contradictory outreach.                                                                                                                                       |
| Idempotency Engine         | Every outbound send, calendar booking, database update, and billing trigger requires a unique idempotency key to prevent duplicate sends or double charges.                                                                                                                                                  |
| Persistent Circuit Breaker | Emergency halt controls at global, tenant, campaign, or channel level freeze schedulers indefinitely until authorized human issues a resume command in Slack. A TTL must never re-arm a safety halt.                                                                                                         |

**Autonomy Ladder & Governance Bands:**

| Authority Band                   | Operational Capability                                                                          | Promotion Requirement                                       | Demotion Trigger                                             |
| -------------------------------- | ----------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------ |
| Band 1: Observe & Report         | Agent reads, analyzes, drafts, and recommends. Zero autonomous external actions.                | Default baseline for all new action classes.                | Immediate on any critical runtime fault.                     |
| Band 2: Class Approval (One-Tap) | Concrete action card queued in Slack for one-click human authorization.                         | 50 consecutive clean human approvals (≥95% approval rate). | ≥1 policy violation or approval rejection spike (>5%).      |
| Band 3: Bounded Auto-Execution   | Reversible, low-risk actions execute autonomously within hard-coded rate, send, and spend caps. | 250+ clean executions with <2% dispute/reversal rate.       | Any customer dispute, unhandled error, or reversal incident. |
| NEVER AUTONOMOUS                 | Pricing terms, billing charges, refunds, legal replies, contract closing.                       | No promotion path.                                          | Hardcoded invariant in deterministic code.                   |

**Central Slack Hub Cockpit:**

| Slack Channel         | Primary Purpose                                                                                                                                                                                       |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| #blackink-command     | Executive cockpit: macro queries, portfolio health, emergency global pause/resume, cross-client alerts.                                                                                               |
| #blackink-setter      | Human conversation cockpit: 1-screen context cards for hot leads, opener suggestions, objection battle-cards, dial tasks.**Also the email approval queue for all early-client outbound sends.** |
| #sales-replies        | Real-time inbound communication stream with intent classification tags and one-tap triage buttons.                                                                                                    |
| #dial-tasks           | Prioritized daily calling queue with direct dial lines, timezone calculations, Owner Visibility Score, and county rank.                                                                               |
| #client-{name}-growth | Dedicated tenant growth workspace: quota pacing, active campaign stats, hot leads.                                                                                                                    |
| #client-{name}-launch | Dedicated onboarding channel: 15-state checklist tracking, missing asset alerts, preflight status, launch approvals.                                                                                  |
| #blackink-qa          | Health and resilience: deliverability alerts, domain reputation drops, dead-letter queues, failed webhooks, cross-tenant leakage reports.                                                             |
| #blackink-economics   | Financial channel: per-tenant cost ledgers, contribution margins, paid wallet consumption, Economics Governor flags.                                                                                  |

### 5.2 Specialist Agent Profiles

| Agent                     | Core Engine Ancestry           | Primary Mission                                                                    |
| ------------------------- | ------------------------------ | ---------------------------------------------------------------------------------- |
| 1. Launch Agent           | Launch OS / Golden Cloner      | Rapid onboarding, preflight validation & governed launch.                          |
| 2. Prospecting Agent      | Hunter / Scout                 | Entity resolution, multi-LLC aggregation & owner scoring.                          |
| 3. Campaign Agent         | Cora (Draft) + Relay (Execute) | Multi-touch sequencing, Owner Visibility Score delivery & approval-gated dispatch. |
| 4. Lead Agent             | Reply Triage Agent             | <5 min inbound triage, context assembly & weekend mode.                            |
| 5. Reactivation & Nurture | Reactivation Engine            | Dead-lead revival, timing memory & churn save workflows.                           |
| 6. Referral Agent         | Referral & Partner Desk        | B2B partner ecosystems, Realtor pipelines & Ryse attach.                           |
| 7. Economics & Growth     | Vera                           | Financial reconciliation, cost ledger & governor bands.                            |
| 8. QA / Watchdog Agent    | Watchdog Sentinel              | Tenancy isolation, deliverability health & self-healing.                           |
| 9. Setter Copilot         | Closer Cockpit                 | 1-screen call context cards, opener logic & rubric scoring.                        |

#### 1. Launch Agent

**Mission:** Transform a newly signed property management contract into a fully configured, compliant, and active client tenant within 72 hours.

**Core Responsibilities:** Coordinates the 15-state onboarding checklist; clones the Golden Client database template; assigns pre-warmed sending domains, Instantly sub-workspaces, and dedicated local Telnyx phone numbers; evaluates preflight checks; auto-generates the First-14-Days growth plan; executes governed offboarding teardowns.

**Autonomy Bounds:** Band 1 (Default) observes checklist progress; Band 2 (One-Tap) queues campaign arming once preflight is 100% green. Hard Stop: cannot launch campaigns if any preflight check evaluates red or payment authorization is unverified.

#### 2. Prospecting Agent (Hunter / Scout)

**Mission:** Maintain an unexhausted pipeline of qualified property owners without exhausting market territory or violating compliance boundaries.

**Core Responsibilities:** Ingests public county deed recordings, tax assessor rolls, and DBPR license registries across target Florida counties (Hillsborough and Pinellas first); resolves corporate LLC owners back to true individual managing members; calculates Owner Score = `(Verified Doors × 15) + (Distress Multiplier × 20) + (In-Market Proximity × 10) − (Entity Fragmentation Penalty)`. Operates targeted acquisition sub-recipes: Portfolio Intercept (5+ units), Retiring-Broker Radar, Absentee Owners, Eviction Dockets, Review-Mining switchers.

**Autonomy Bounds:** Band 1 extracts data, scores, and drafts candidate queues; Band 2 enqueues top-decile verified owner batches. Hard Stop: hard-blocked from querying or targeting any entity on active client suppression or non-poach lists.

#### 3. Campaign Agent (Cora & Relay)

**Mission:** Coordinate, execute, and dynamically optimize multi-touch growth campaigns across email, human phone prompts, and LinkedIn without requiring human copy rewrites for every touch.

**Core Responsibilities:** Generates personalized 5-touch outbound sequences using Owner Visibility Score reports, county ranking data, and fee-stack proofs (ghost-shopper and Sendspark permanently deferred — see §3.1.3); queues every email draft for human approval in `#blackink-setter` until 50 clean approvals per template class are achieved; enforces per-mailbox rate limits (30–50 sends/day); manages sending domain rotation.

**Autonomy Bounds:** Band 1 generates drafts for human approval; Band 2 dispatches approved sequence templates autonomously after 50 clean reviews; Band 3 executes minor copy and timing optimizations within send caps. Hard Stop: cold outbound SMS is hard-blocked; cannot modify commercial pricing terms or bypass daily mailbox limits.

#### 4. Lead Agent / Reply Triage

**Mission:** Ingest, classify, and route 100% of inbound communications within 5 minutes.

**Core Responsibilities:** Parses inbound messages across 10 operational intent classes; executes deterministic opt-out writes immediately upon detecting unsubscribe intent; generates 1-screen context cards for setter/closer queues; enforces tiered response SLAs (15/60/240 minutes); manages approved Lead template catalog (10 case types, 3 complete scripts). Lead never discusses management fees, lease terms, rent advice, or screening criteria — those escalate to the client.

**Input Interfaces:** Inbound email webhooks, Telnyx SMS webhooks (future), website concierge form submissions. **Output Interfaces:** Slack context cards, KB replies, Google Calendar / Microsoft Graph booking links, compliance suppression writes.

**Autonomy Bounds:** Band 1 classifies and routes drafts; Band 2 auto-dispatches approved KB answers on confirmation; Band 3 sends high-confidence KB responses (≥90% confidence). Hard Stop: all legal threats, opt-outs, and negative complaints bypass automated generation immediately.

#### 5. Reactivation & Nurture Agent

**Mission:** Convert old inquiries, unclosed proposals, no-shows, and churn-risk properties into signed revenue.

**Core Responsibilities:** Maintains timing memory store for future re-engagement dates; executes Win-Back recipe against client historical dead leads (email only — no SMS, no AI voice); coordinates no-show recovery workflows; operates Churn Tripwire save workflow.

**Autonomy Bounds:** Band 1 flags upcoming dates and drafts messages; Band 2 fires win-back batches on one-click approval; Band 3 schedules future-dated touches within established cadences. Hard Stop: cannot contact any property owner currently in an active client's active management database.

#### 6. Referral Agent

**Mission:** Establish a compounding local referral network through relationships with real estate agents, vendors, lenders, and industry partners.

**Core Responsibilities:** Identifies and prioritizes local professional partners; executes B2B partner sequences; manages Partner Menu enrollment workflows; operates the Referral Credit Ledger; captures client wins as marketing proof.

**Autonomy Bounds:** Band 1 drafts partner outreach and logs referral sources; Band 2 enqueues testimonial and referral requests upon verified milestones. Hard Stop: all vendor agreements, legal fee-sharing contracts, and financial disbursements require human execution.

#### 7. Economics & Growth Agent (Vera)

**Mission:** Track unit economics, manage client quotas, enforce deterministic spending governors, and calculate per-tenant contribution margins.

**Core Responsibilities:** Operates the Opportunity Quota Engine; enforces the Deterministic Economics Governor (Green/Yellow/Orange/Red bands); manages the Paid Wallet Engine (hard caps $150–$300 initial risk); calculates Client Health Score; monitors collections and billing health; compiles monthly books-ready financial export packs.

**Autonomy Bounds:** Band 1 calculates economics and drafts reports; Band 2 triggers dunning recovery emails and queues budget reallocations. Hard Stop: cannot modify pricing tiers, issue cash refunds, or alter wallet spend ceilings without human authorization. Missing data returns `UNKNOWN`/`ABSTAIN` — never silently becomes zero.

#### 8. QA / Watchdog Agent

**Mission:** Ensure system integrity by actively monitoring infrastructure health, validating tenant isolation, enforcing suppression rules, and auto-recovering from transient faults.

**Core Responsibilities:** Executes nightly adversarial cross-tenant data leakage tests; audits suppression integrity; monitors deliverability across all 20 sending domains; evaluates system heartbeats; auto-adjudicates appointment disputes; implements self-healing routines (domain quarantine, DLQ re-runs, container restarts).

**Autonomy Bounds:** Band 1 monitors and posts alerts; Band 2 recommends domain rotations and DLQ re-runs; Band 3 automatically quarantines degraded domains. Hard Stop: cannot override compliance gate errors or dismiss security assertion failures.

#### 9. Setter Copilot

**Mission:** Maximize conversation-to-booking conversion rate of human setters by generating real-time context briefs, recommended talk-tracks, and post-call analysis.

**Core Responsibilities:** Assembles 1-Screen Context Cards for every scheduled call (Owner Visibility Score, county rank, 3 weakest categories, engagement history, suggested openers, objection battle-cards); manages daily calling queue in `#blackink-setter`; ingests call recordings and transcripts, scoring against rubrics; integrates with Referral Agent upon positive sales close.

**Input Interfaces:** Google Calendar / Microsoft Graph booking payloads, call recording webhooks, prospect engagement telemetry, CRM thread histories.

**Autonomy Bounds:** Band 1 generates call briefs and drafts follow-up notes; Band 2 enqueues drafted post-call summaries for one-click dispatch. Hard Stop: all verbal discovery conversations, qualification decisions, and contract negotiations remain strictly human.

### 5.3 Cross-Agent Orchestration, Work-Orders & Locking Protocols

```sql
-- Schema: src/db/migrations/008_work_orders.sql
CREATE TABLE agent_work_orders (
  action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
  entity_id UUID NOT NULL,
  opportunity_id UUID,
  agent_id VARCHAR(50) NOT NULL,
  action_class VARCHAR(100) NOT NULL,
  autonomy_band VARCHAR(20) NOT NULL,
  risk_class VARCHAR(20) NOT NULL,
  confidence_score NUMERIC(5,2) NOT NULL,
  payload_hash VARCHAR(64) NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  status VARCHAR(50) DEFAULT 'QUEUED',
  approved_by_user_id VARCHAR(100),
  approval_timestamp TIMESTAMP WITH TIME ZONE,
  execution_receipt JSONB,
  due_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

Distributed Redis leases prevent competing agents from executing contradictory actions on the same entity:

```python
# Location: src/core/orchestration/lease_manager.py
import redis
import hashlib
import json
from datetime import datetime, timezone

class EntityLeaseManager:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def acquire_entity_lease(self, client_id: str, entity_id: str, agent_id: str, ttl_seconds: int = 300) -> bool:
        lease_key = f"lease:{client_id}:{entity_id}"
        lease_payload = json.dumps({
            "agent_id": agent_id,
            "acquired_at": datetime.now(timezone.utc).isoformat()
        })
        acquired = self.redis.set(lease_key, lease_payload, nx=True, ex=ttl_seconds)
        return bool(acquired)

    def release_entity_lease(self, client_id: str, entity_id: str, agent_id: str):
        lease_key = f"lease:{client_id}:{entity_id}"
        current_lease = self.redis.get(lease_key)
        if current_lease:
            data = json.loads(current_lease)
            if data.get("agent_id") == agent_id:
                self.redis.delete(lease_key)
```

**NOTE:** The TTL on an entity lease must not be confused with a safety halt TTL. A lease TTL expiring is normal operational behavior and permits another worker to proceed. A safety halt (campaign pause) persists indefinitely in both Redis and PostgreSQL and is never re-armed by TTL expiry — only by explicit authorized resume.

**End-to-End Inter-Agent Execution Trace:**

| Step | Agent                  | Action                                                                                                                                                                            |
| ---- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Prospecting Agent      | Ingests county records; resolves multi-LLC owners; calculates Owner Score; writes prospect to companies and contacts.                                                             |
| 2    | QA / Watchdog Agent    | Compliance lint: verifies DNC status, zero non-poach conflicts, tags record as`EMAIL_COLD_ELIGIBLE`.                                                                            |
| 3    | Campaign Agent         | Generates Owner Visibility Score PDF from public observable signals; queues Email 1 draft in`#blackink-setter` for human approval.                                              |
| 4    | Lead Agent             | Prospect replies "Interested, how does this work?"; classifies as`HOT_LEAD` (96% confidence); halts outbound sequence; alerts `#blackink-setter`.                             |
| 5    | Setter Copilot         | Compiles 1-screen context card (portfolio size, Owner Visibility Score, county rank, engagement data); closer runs discovery call; prospect books on Google Calendar.             |
| 6    | Launch Agent           | Agreement e-signs; fires tokenized Onboarding Portal link; tracks 15 states; provisions isolated database schema; arms client campaigns.                                          |
| 7    | Economics Agent (Vera) | Nightly sync confirms newly signed property agreement (`door_signed`); triggers 50% initial Stripe ACH charge; compiles Evidence Packet PDF; schedules Day 60 clawback monitor. |

### 5.4 Unified Memory Spines & Continuous Learning Loops

| Learning Loop                | Data Stored                                                                                                |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------- |
| 1. Operational Memory        | Active tasks, deadlines, lease states, dependencies.                                                       |
| 2. Conversation Memory       | Complete communication history, objections, commitments, sentiment tags across all channels.               |
| 3. Standing Client Rules     | Tenant-specific brand guidelines, forbidden claims, geographic boundaries, target fee structures.          |
| 4. Counterfactual Memory     | Human revisions, overrides, and rejected agent recommendations alongside stated reasons.                   |
| 5. Win/Loss Autopsy          | Structured teardowns of won vs. lost opportunities (lead source, door count, messaging angle, objections). |
| 6. Experiment Registry       | Every copy, subject line, and timing variant tested with sample sizes and statistical significance.        |
| 7. Source Quality Loop       | Match rate, cost per usable record, and signed doors per source dollar across data vendors.                |
| 8. Closing / Transcript Loop | Call recordings, rubric scores, and objection handling data to train Setter Copilot.                       |

**Cross-Tenant Privacy Protection:** All learned playbooks are sanitized of PII and tenant-specific business data before optimization weights transfer across clients as generalized statistical heuristics.

**Note on MAX / Learning Loops:** Buy/build the outcome schema, columns, entity key, and interfaces read by learning loops in September. Price loop logic separately and hold for a December decision when sufficient outcomes exist.

### 5.5 System Reliability, Circuit Breakers & Incident Handling

| Failure Scenario              | Detection Trigger                                                   | Recovery Protocol                                                                                                                                                              |
| ----------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Third-Party API Outage        | Provider returns 5xx or times out for 3 consecutive requests        | Fails gracefully to cached fallback data; labels outputs as DEGRADED; queues retries with exponential backoff. (Rent Analysis Bot API adapters are disabled by default — Q1.) |
| Stale Data or Missing Key     | Data older than 30-day refresh window or required key is null       | Returns explicit`UNKNOWN` or `ABSTAIN`; prevents zero-filling; alerts `#blackink-qa`.                                                                                    |
| Persistent Task Failure       | Job fails across 3 retry attempts                                   | Moves payload to Dead-Letter Queue (DLQ); creates priority incident ticket in Slack.                                                                                           |
| Domain Reputation Drop        | Bounce rate >3% or spam complaints >0.08% in rolling 48-hour window | Instantly quarantines degraded domain; routes traffic to warmed backup pool.                                                                                                   |
| Cross-Tenant Isolation Breach | Adversarial test detects record with conflicting`client_id`       | Trips Emergency Circuit Breaker; freezes all outbound dispatch; alerts P0 to`#blackink-command`.                                                                             |
| Emergency Operator Halt       | Human triggers`/halt` in Slack                                    | Persistently freezes all schedulers and message queues in Redis/DB until explicit authorized resume. TTL must never re-arm this halt.                                          |

### 5.6 Full-System Acceptance Standard (Definition of Done)

The Blackink Governed Nine-Agent Workforce is certified production-ready upon passing the September 30 End-to-End Multi-Agent Acceptance Contract:

| Verification Scenario        | Required Demonstrated Behavior                                                                                                                                                 |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Cold Email Dispatch          | Campaign Agent generates Owner Visibility Score PDF; queues draft in`#blackink-setter`; email dispatches from tenant-isolated warmed mailbox only after human Approve click. |
| Inbound Reply Classification | Lead Agent classifies`HOT_LEAD` within 5 minutes; halts outbound sequence; builds context card; sets SLA timer in `#blackink-setter`.                                      |
| Deterministic Opt-Out        | `UNSUBSCRIBE` intent triggers immediate `is_opted_out = TRUE` write; all sequence dispatches halt; no human approval required for the suppression write.                   |
| Compliance Gate Enforcement  | Contact in active client's PM book triggers non-poach suppression;`non_poach_suppressed` event logged; no email dispatched.                                                  |
| Cold SMS Block               | Any code path attempting outbound SMS to unconsented contact fails at DB constraint, application linter, and CI/CD build gate.                                                 |
| Calendar Booking             | Google Calendar / Microsoft Graph booking webhook fires;`meeting_booked` event created; email confirmation + ICS dispatched within 60 seconds.                               |
| Settlement Execution         | Attended appointment verified against 4-rule bar; Stripe ACH drafted; Evidence Packet PDF auto-compiled; 60-day clawback job scheduled.                                        |
| Circuit Breaker              | `/halt` in Slack freezes all tenant outbound queues across server restarts until explicit `/resume` executed.                                                              |
| Cross-Tenant Isolation       | Adversarial CI/CD test injecting cross-tenant synthetic records passes with zero leakage detected.                                                                             |
| Backup & Restore             | Daily PostgreSQL backup completes; full restore executed in staging without production PII exposure.                                                                           |
