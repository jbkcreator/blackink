# UNIFIED SYSTEM SPECIFICATION & IMPLEMENTATION BLUEPRINT

## Project Blackink — Implementation Blueprint

**Client** Josh Kantor, Blackink
**Lead Developer** Hari Krishnan (heu.ai)

## 1. Master Data Contracts & Ingestion Variable Specification

All upstream prospect and market intelligence delivered by the data pipeline must map directly to the `raw_prospect_pipeline` staging schema before ingestion into the primary database.

### A. Target Company & Market Entity Schema

- **company_id** (UUID / String, Primary Key): Deterministic SHA-256 hash generated from normalized domain.
- **company_name** (String, NOT NULL): Legal operating or DBA name of the property management company.
- **website** (String, NOT NULL): Validated corporate website URL.
- **domain** (String, UNIQUE, NOT NULL): Normalized apex domain (e.g., `suncoastpm.com`).
- **market_metro** (String, NOT NULL): Geographic target market (e.g., `Tampa-St. Petersburg`, `Orlando`, `Miami-Dade`).
- **door_count_est** (Integer, NOT NULL): Estimated residential doors under management.
- **current_pm_software** (String, Nullable): Ingested primary PMS platform (`AppFolio`, `Buildium`, `Propertyware`, `Rent Manager`, `Other`, `UNKNOWN`).

### B. Two-Contact Structure (Owner/Broker & Operations)

- **contact_id** (UUID / String, Primary Key): Unique identifier per contact.
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

- **audit_speed_score_sec** (Integer, Nullable): Ghost-shopper response time recorded in seconds.
- **audit_loss_dollars_est** (Integer, Nullable): Modeled annual revenue loss based on response latency and door count.
- **personalized_video_id** (String, Nullable): Sendspark dynamic merge token.
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
AND (p.audit_speed_score_sec IS NOT NULL OR p.personalized_video_id IS NOT NULL)
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
- **Engine Architecture (Row + Adapter):** Every revenue engine is modeled as a database configuration row (defining router parameters, templates, sequence steps, county locks, tiers, and holdout percentages) coupled to a dedicated execution adapter.
- **Deterministic Money and Compliance:** No autonomous agent or LLM makes financial, billing, legal, or compliance decisions. The compliance gate, non-poach suppression, dispute rules, and Stripe settlement pipelines run on strict, unbypassable code.
- **Tenant Isolation & Security:** Data, sending reputation, credentials, and memory structures are strictly partitioned by `client_id`. Cross-client leakage tests run automatically in CI/CD.

## 3. Master Week-by-Week Implementation Sprints

### Week 0 (Aug 31 – Sept 2, 2026): Step 1 Reuse, Critical Platform Remediation & Compliance

**Phase Objective:** Gate 1 Proof, Fork & Baseline Stabilization

Week 0 serves as the foundational validation gate for the entire Blackink operating platform. Before deploying net-new campaign logic, commercial sequences, or client-facing onboarding tools, the technical team isolates and audits all reusable infrastructure across prior builds. This phase forks core execution pipelines, eliminates known legacy defects, deploys the centralized Slack Agent Hub, establishes the upstream data pipeline contract with Akrash, and files official carrier brand and campaign registrations. Week 0 operates as a strict proof gate: work focuses on establishing tenant isolation, persistent safety halts, deterministic compliance gates, and verified truth states so that downstream marketing and settlement modules build upon a stabilized, bug-free platform.

#### 3.0.1 Core Asset Audit & Module-by-Module Reuse Ledger

The development team executes a comprehensive code audit across existing agent frameworks, classifying components into distinct portability tiers:

**Direct Porting (Clean Fork):**

- Outbound Drafting Patterns: Language models and structured output formatters from drafting engines.
- Event-Driven Execution Harnesses: Asynchronous queue runners, dispatchers, and state machines.
- Suppression & DNC Scrubbing: Deterministic matching routines connecting to state and national Do-Not-Call registries.
- Slack Interactive State Machines: Interactive Block Kit button handlers (`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`) and modal submission listeners.

**Refactored Components:**

- Data Normalization & Ingestion: Adapting entity resolution routines to separate generic properties from corporate LLC owners holding multi-unit portfolios.
- Outbound Dispatch Adapters: Decoupling pooled sending identities into strict, tenant-isolated mailbox assignments.
- Audit Compilation Pipelines: Modularizing PDF loss-report generators to consume property management speed metrics rather than general distress scoring.

**Pattern-Only Adoptions:**

- Multi-Tenant Data Schema: Establishing row-level tenant keying (`client_id`) across all primary tables, views, and Redis cache keys.
- Deterministic Gate Enforcement: Hard-coded pre-send policy checks that completely bypass LLMs when evaluating suppression, quiet hours, and channel eligibility.

A formal Reuse Ledger is compiled and delivered at the conclusion of Week 0, documenting components ported, architectural refactors completed, and automated test pass rates.

#### 3.0.2 Platform Defect Remediation & Core Infrastructure Hardening

To prevent legacy runtime bugs from propagating into Blackink's production environment, Week 0 addresses four critical system vulnerabilities:

**A. Vera Silent-Zero Remediation:** In legacy reporting pipelines, database queries or API lookups returning null or missing financial values defaulted silently to `0`. In an outcome-based billing architecture where invoices trigger upon attended meetings and verified contracts, a silent zero results in unbilled revenue and corrupts accounting truth. **The Fix:** The data-reconciliation layer is refactored to enforce strict tri-state logic. When an external integration (Stripe, calendar logs, PM software read feeds) is unreachable, degraded, or returns missing values, the system explicitly returns `UNKNOWN` or `ABSTAIN`. Downstream settlement routines halt automatically and alert administrators rather than assuming zero payable activity.

**B. Relay Persistent Halt & TTL Re-Arm Patch:** Legacy execution state machines contained a vulnerability where emergency pause commands would automatically re-arm and resume outbound message queues after an internal Time-To-Live (TTL) timer expired. **The Fix:** The scheduler and queue worker state machines are updated so that a global, client-level, or campaign-level pause persists indefinitely in Redis and PostgreSQL. Outbound queues remain locked until an authorized administrator explicitly executes a cryptographic resume command in Slack.

**C. Cora Queue Throttling & Batch Burst Protection:** Draft generation engines previously lacked dynamic backpressure controls, causing draft queues to overwhelm human reviewers during large prospect batch ingestions. **The Fix:** Strict queue bounds and pacing throttles are embedded into draft orchestrators. Generation limits automatically pause new drafting once unreviewed Slack approval queues reach capacity, resuming only as human reviews clear items.

**D. Standalone Hunter Entity Resolution Deployment:** To prevent heavy cross-table data joins from blocking the primary web application and API threads, entity resolution is decoupled. **The Fix:** A standalone worker droplet is provisioned exclusively for Hunter-style entity matching. It runs nightly asynchronous background sweeps, resolving corporate names, registered agents, and individual property owners across fragmented multi-property LLC portfolios.

#### 3.0.3 Slack Agent Hub (@Blackink) & Interactive Cockpit Deployment

Week 0 establishes @Blackink as the centralized operating surface inside Slack, ensuring full visibility and control over all background workflows:

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
- `#blackink-setter`: Human conversation cockpit rendering 1-screen context cards for high-intent owner leads.
- `#sales-replies`: Real-time inbound reply stream routing prospect email/SMS responses with automated intent tags.
- `#dial-tasks`: Prioritized daily phone queue populated with company background, response latencies, and direct lines.
- `#blackink-qa`: Real-time system health logs, API heartbeat failures, domain reputation deltas, and cross-tenant leakage test alerts.
- `#blackink-economics`: Rollup dashboards tracking customer acquisition costs, channel unit economics, and wallet caps.

**Payload-Bound Hash Verification:** Every interactive Slack card (`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`) is cryptographically bound to a SHA-256 hash of the exact message payload, recipient identifier, and configuration state. If a draft payload or template is modified in the background while awaiting review, clicking an outdated Slack button is rejected by the backend, preventing stale or altered messages from executing.

#### 3.0.4 A2P 10DLC Carrier Registration & Compliance Infrastructure

To ensure message deliverability and carrier regulatory compliance, Week 0 initiates the carrier verification clock through Telnyx/The Campaign Registry (TCR):

**Brand Registration:** Legal Entity: HEU AI LLC. Address: 971 US Highway 202N Ste N, Branchburg, NJ 08876. Entity Type: Private Company, LLC (New Jersey). Vertical: Real Estate / Professional Services.

**Campaign Filing Details:** Use Case Classification: Mixed: Customer Care + Account Notification (Strictly non-marketing to secure lower carrier fees and eliminate promotional vetting friction). Campaign Description: "Blackink provides scheduling and inbound-response services to residential property management firms. Messages are sent only to property owners who have (a) initiated an SMS conversation with us or (b) booked an appointment through us. Message types: appointment confirmations, reminders, rescheduling links, and replies to owner-initiated questions. No promotional or cold outreach is sent by SMS."

**Opt-In & Consent Description:** "Opt-in occurs in one of two ways: (1) The property owner sends an SMS to a Blackink number, which constitutes consent to reply. (2) The property owner books an appointment via a web form or during a phone/email conversation and enters their mobile number, with a consent statement displayed at the point of collection: 'By providing your mobile number you agree to receive appointment confirmations and reminders by SMS. Reply STOP to opt out, HELP for help. Msg & data rates may apply.' No numbers are purchased, scraped, or imported from third-party lists for SMS."

**Keywords:** `STOP`, `END`, `CANCEL`, `UNSUBSCRIBE`, `QUIT` for automated opt-out; `HELP` for support routing.

**Code-Level Compliance Enforcement:** CI/CD build test fails immediately if code attempts an outbound SMS to any contact where `inbound_sms_count == 0 AND booked_appointment_id IS NULL`. Quiet hours strictly enforced (no SMS delivery between 9:00 PM and 8:00 AM recipient local time). Outbound templates require the `{client_firm}` tag, ensuring the recipient clearly sees the operating company name.

#### 3.0.5 Section 8 Data Pipeline Interface & Akrash Staging Handoff

Week 0 finalizes the formal operational data contract between the upstream data team (Akrash) and the core platform (Hari), eliminating manual CSV spreadsheets:

- **Staging Database Ingestion:** Akrash is provisioned restricted access to write prospect data directly into the `raw_prospect_pipeline` PostgreSQL staging table.
- **Mandatory Schema Fields:** Ingested records must contain `company_id`, `company_name`, `domain`, `market_metro`, `door_count_est`, two contacts (`contact_role_type` as `OWNER_BROKER_MD` or `OFFICE_MANAGER_OPS`), verified email, direct phone, and audit timestamps.
- **Ready for Campaign Gate:** Upstream records remain in quarantine until background evaluators confirm email verification, DNC clearance, global opt-out clearance, and non-poach cross-suppression checks.
- **DNS & Warmup Delegation:** DNS access is delegated to configure SPF, DKIM, and DMARC across 20 dedicated domains (40 mailboxes), initializing domain warmup schedules ahead of campaign launch.

#### 3.0.6 Week 0 Acceptance Criteria & Definition of Done

Week 0 concludes and clears Gate 1 when all of the following verifiable conditions are met:

1. **Live Slack Cockpit:** The @Blackink Slack app is active in the designated workspace, posting native action cards and processing interactive button clicks with payload-bound hash verification.
2. **Defect-Free Health Reporting:** Vera health jobs execute across staging databases, outputting `UNKNOWN` or `ABSTAIN` states on missing inputs with zero silent zero returns.
3. **Verified Persistent Halt:** An emergency pause triggered via Slack persistently halts background execution queues across server restarts until an authorized resume is executed.
4. **Entity Resolution Pipeline:** Hunter standalone workers successfully resolve a sample batch of property owner entities across fragmented LLCs.
5. **Submitted A2P 10DLC Filing:** Brand and Mixed Campaign registrations are officially submitted to TCR, with automated CI/CD tests blocking cold outbound SMS.
6. **Signed Data Interface Contract:** The Section 8 Data Interface Specification is approved, and the `raw_prospect_pipeline` database staging schema is live.
7. **Documented Reuse Ledger:** A complete module-by-module accounting of ported assets, architectural refactors, and test coverage is delivered.

### Week 1 (Sept 1 – Sept 11, 2026): Sprint 1 — Phase 0 + Marketing & Demo Layer

**Milestone Standard:** September 11 Marketing Live (Live Outbound Campaigns, Ghost-Shopper Audits, Sendspark Dynamic Video Hooks, Automated Booking, and Demo Kit Sandbox)

Week 1 transitions Blackink from foundational scaffolding into a live demand-generation engine. The primary objective is to make the outbound sales and marketing systems fully functional by September 11, enabling live pitches, mystery-shop response audits, personalized video delivery, automated calendar bookings, and interactive software demonstrations on real prospect data. The build sequence decouples frontend marketing and demo assets from downstream tenant settlement rails, allowing cold outreach and demo bookings to run against target property management companies in Florida while multi-tenant client fulfillment and settlement engines are completed in parallel.

```
WEEK 1 COMPLETE CAMPAIGN & DEMO LAYER ARCHITECTURE (GO-LIVE: SEPTEMBER 11, 2026)

[Raw Prospect Data: 500-1,000 PMs] ──► [Deterministic Compliance Gate] ──► [Ghost-Shopper Inbound Bot]
        │                                                                          │
[Non-Poach / DNC / Waterfall]                                            [Logs Latency & Speed]
        │                                                                          │
        ▼                                                                          ▼
[Personalized Outbound Sequences] ◄── [Sendspark Video Merge Engine] ◄── [Dynamic PDF Loss Report]
        │
        ├──► [Email 1: Audit + Video + Reply-YES Micro-Ask]
        ├──► [Day 1–2 Phone Call Task ──► Enqueued into Slack #dial-tasks]
        ├──► [Email 2: Fee-Stack Revenue Opportunity Map]
        ├──► [LinkedIn Deep-Link Handoff ──► Manual Clipboard Copy]
        ├──► [Email 3: Metro Speed Index & Market Rank Angle]
        └──► [SMS Nudge ──► Unlocks Strictly Post-Engagement]
        │
        ▼
[Inbound Reply Bridge ──► Real-Time Slack #sales-replies]
        │
        ▼
[Calendly / GCal Direct Booking ──► Show-Rate Reminder Cascade]
        │
        ▼
[Pre-Demo Lead-In Email (30m Prior) + Rent Analysis Bot SMS Pitch Weapon]
        │
        ▼
[Sales Demo Kit: Permanent Golden Client Sandbox Live Rehearsal]
```

#### 3.1.1 Data Spine, Entity Models & Ingestion Pipeline

The foundation of Week 1 is the generic events ledger and the primary entity data model. Every downstream component—from compliance checks to outbound dispatch and meeting attribution—reads from and writes to this schema.

```sql
-- Location: src/db/migrations/001_core_spine.sql
-- 1. GENERIC EVENTS STREAM (Shared Ledger of Record)
CREATE TABLE events (
event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
client_id UUID NOT NULL, -- Scoped per tenant; Blackink self-marketing uses dedicated internal client_id
owner_id UUID,
property_id UUID,
campaign_id UUID,
event_type VARCHAR(100) NOT NULL, -- 'touch_sent', 'reply_received', 'audit_generated', 'meeting_booked', 'partner_ryse_enrollment'
source VARCHAR(50) NOT NULL, -- 'cold_outbound', 'ghost_shopper', 'website_inbound', 'direct_mail'
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
market_metro VARCHAR(100) NOT NULL,
door_count_est INTEGER NOT NULL DEFAULT 0,
current_pm_software VARCHAR(100) DEFAULT 'UNKNOWN',
status VARCHAR(50) DEFAULT 'PROSPECTING', -- 'PROSPECTING', 'ENGAGED', 'DEMO_BOOKED', 'CLIENT', 'EXCLUDED'
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

-- 4. PM PROFILE (Future-Proofing Metadata Schema)
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

-- Indices for performance and compliance enforcement
CREATE INDEX idx_events_client_type ON events(client_id, event_type, occurred_at);
CREATE INDEX idx_contacts_lookup ON contacts(email, company_id, compliance_eligibility);
CREATE INDEX idx_companies_domain ON companies(domain);
```

The ingestion pipeline processes 500–1,000 target property management companies across Florida metros (Tampa/St. Petersburg, Orlando, Miami-Dade). For every target enterprise, the pipeline resolves and normalizes two distinct contacts:

1. **Contact A (`OWNER_BROKER_MD`):** The ultimate decision-maker (Managing Broker, Owner, President, CEO). Receives high-level financial proof: revenue loss metrics, fee optimization maps, and asset-growth angles.
2. **Contact B (`OFFICE_MANAGER_OPS`):** The operational gatekeeper (Operations Manager, Lead Property Manager, Leasing Director). Receives operational velocity proof: speed-to-lead benchmarks, mystery-shopping response timings, and software response gap metrics.

Data from the upstream `raw_prospect_pipeline` staging table is validated against apex domain uniqueness, normalized, and mapped into `companies` and `contacts` records.

#### 3.1.2 Deterministic Compliance Gate & Non-Poach Architecture

Compliance in Blackink is hard-coded into deterministic execution gates. No artificial intelligence or language model is permitted to evaluate consent, opt-out rules, or cross-client suppression.

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
- Human Phone: Task to #dial-tasks            - Transactional SMS: UNLOCKED
- Cold SMS: STRICTLY BANNED (CI Fails Build)  - 10DLC Customer Care Approved
```

**Warm-Channel Waterfall:** Cold prospect outreach is strictly restricted to Email and human telephone tasks. Cold outbound SMS is blocked at the database, application, and CI/CD testing levels. SMS permissions unlock strictly after an inbound message is received or an appointment is confirmed on the calendar.

**Cross-Client Non-Poach Gate:** Read-only connections to active clients' property management software sync current owner rosters into a centralized suppression table. The compliance gate verifies that target domains, owner names, and entity parcels do not match any active client's book, preventing one client from poaching doors from another.

**Metro Allocation Algorithm:** Where multiple property management firms operate within the same metropolitan boundary, target owners are allocated to one client campaign at a time based on portfolio size and operational fit. If outreach remains unacted upon for 30 days, allocation re-evaluates via an automated timer.

**National & State DNC Scrubbing:** Automated pre-send linter queries real-time DNC registries, stripping dial tasks and SMS eligibility from restricted records.

#### 3.1.3 The Outbound Proof Machine: Audit Factory, Personalized Video & Fee-Stack Generator

```
GHOST-SHOPPER AUDIT FACTORY & SENDSPARK DYNAMIC MERGE PIPELINE

[Target PM Website Ingestion] ──► [Ghost-Shopper Inbound Bot] ──► [Submits Structured Owner Inquiry]
                                                                            │
                                                                  [Captures Exact Milliseconds]
                                                                            │
                                                                            ▼
[Dynamic PDF Loss Compiler] ◄── [Calculates Annual Revenue Loss] ◄── [Response Latency Recorded]
        │
        ├──► Injects: Metro Rank, Average Peer Speed, Lost Management Fees
        │
        ▼
[Sendspark Video Integration Engine]
        │
        ├──► Generates Dynamic Video Landing Page: https://watch.blackink.io/v/{company_id}
        ├──► Injects Merge Parameters: {company_name}, {loss_score}, {audit_speed_sec}
        ├──► Renders Animated GIF Thumbnail with Prospect Website Overlay
        │
        ▼
[Outbound Dispatch: Embeds Merge Token & PDF Attachment into Email 1 Payload]
```

**A. Ghost-Shopper Agent** — An automated headless crawler navigates to the target property management firm's public website, locates their owner inquiry or contact form, and submits a standardized, professional owner inquiry. The bot records the exact millisecond of submission and establishes an inbound webhook and IMAP email listener. When the target company replies, the listener captures the timestamp, calculates total elapsed seconds (`audit_speed_score_sec`), and logs the raw interaction into the events table. If no reply is detected within 24 hours, the record is flagged as `UNRESPONSIVE_OVER_24H`.

**B. Dynamic PDF Loss Report Compiler** — Consumes the ghost-shopper latency score and calculates modeled annual revenue leakage:

> Estimated Lost Inquiries = Estimated Monthly Leads × (1 − e^(−0.0005 × Latency Seconds))
>
> Annual Lost Revenue = Estimated Lost Inquiries × (Average Monthly Management Fee × 12) × Average Door Retention (Years)

Compiles a branded, 2-page executive PDF report detailing: (1) exact timestamped audit log of their form submission vs. first response; (2) geographic metro comparison (e.g., "You responded in 4 hours 12 minutes; Top 10% in Tampa respond in under 9 minutes"); (3) estimated annual management and leasing revenue lost to competing managers who respond faster.

**C. Sendspark Dynamic Video Merge-Tag Integration** — Integrates with Sendspark via REST API to eliminate heavy, slow on-server video rendering. Dynamically generates personalized video landing pages using merge-field parameters (URL pattern: `https://watch.blackink.io/v/{company_id}?company={company_name}&speed={audit_speed_score_sec}&loss={audit_loss_dollars_est}`). Embeds an animated GIF thumbnail in outbound emails showing a dynamic preview of their website overlaid with their speed audit score. Sendspark engagement webhooks (`video_watched_50_percent`, `video_completed`) fire into Blackink's webhook receiver, logging events and notifying sales reps in real time.

**D. Fee-Stack One-Pager Generator (ADD-8-Lite)** — Discovery proof artifact mapping uncollected fee lines and ancillary margins using the shared templated merge pipeline. Highlights common fee leakage points in property management (uncollected lease renewal fees, maintenance markups, tenant setup fees, pet rent share, and resident benefits packages). Visualizes the immediate revenue lift achieved by activating ancillary partner programs (such as Ryse rent advances and utility concierge integrations).

#### 3.1.4 Multi-Touch Outbound Sequencer & Human Bridge

| Sequence Step | Channel & Mechanism           | Timing      | Content Focus & Psychological Angle                                                                                                                               | Verification & Governance Rule                            |
| ------------- | ----------------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| Touch 1       | Cold Email (Direct)           | Day 0       | Speed Loss Audit + Sendspark Video: Delivers personalized PDF audit, dynamic video link, and low-friction micro-ask ("Reply YES to see where you rank in Tampa"). | Verified corporate email only; tracking pixel active.     |
| Touch 2       | Human Phone Call (Slack Task) | Day 1–2    | Audit Follow-up Call: Automated prompt created in#dial-tasks for setter/closer to reference video view data and response audit findings.                          | Verified direct line; local calling hours enforced.       |
| Touch 3       | Cold Email (Direct)           | Day 4       | Fee-Stack Opportunity: Introduces the ADD-8-Lite fee analysis showing uncollected ancillary revenue and Ryse rent advance partnership benefits.                   | Threaded to Email 1; verifies no prior opt-out or reply.  |
| Touch 4       | LinkedIn Deep-Link (Manual)   | Day 7       | Executive Peer Networking: Generates target profile URL and copies tailored connection note to setter's clipboard with zero automated browser scraping.           | Logged as manual task; no headless browser automation.    |
| Touch 5       | Cold Email (Direct)           | Day 10      | Metro Speed Index & Scarcity: References final quarterly Speed Index publication and announces upcoming market territory locks.                                   | Final cold email touch before entering 30-day cooling.    |
| Conditional   | Engaged SMS Nudge (Direct)    | Post-Engage | Direct Scheduling Nudge: Short SMS sent only after recipient clicks an audit link, watches a video, or replies positively to email.                               | Blocked unless explicit engagement event is logged in DB. |

**Interim Reply Bridge & Task Routing (September 11–16):** To bridge the gap before the automated Reply Triage Agent deploys in Week 2, an interim real-time routing engine handles incoming prospect communication — an Inbound Reply Webhook Receiver ingests incoming prospect email and SMS replies instantly; Slack Routing (`#sales-replies`) posts an interactive alert card displaying prospect name, company domain, door count, full message thread history, and one-tap action buttons (`Reply in Thread`, `Book Meeting`, `Mark Opt-Out`); the Setter Queue Bridge (`#dial-tasks`) populates phone tasks with direct dial numbers, local timezone calculations, and Sendspark video watch percentage.

#### 3.1.5 Inbound Conversion, Booking Engine & Show-Rate Cascade

```
BOOKING FLOW & SHOW-RATE CASCADE ARCHITECTURE

[Self-Serve Audit Page / Email CTA] ──► [Calendly / Google Calendar Booking Form]
        │
[Webhook Captures: Name, Work Email, Mobile, Door Count]
        │
        ▼
[Creates `meeting_booked` Event in Database]
        │
        ▼
┌──────────────────────────────────────────────────────┐
│ 1. Instant Branded Confirmation Email + Calendar ICS │
│ 2. Automated SMS Confirmation (Transactional A2P)    │
└────────────────────────┬─────────────────────────────┘
        │
┌──────────────────────────────┴──────────────────────────────┐
▼                                                                ▼
[24 Hours Before Meeting]                          [Morning of Meeting (8:00 AM)]
- Email Reminder with Prep Context                 - Short SMS Ping
- Links to Seeded Demo Video                        - One-Tap Reschedule / Cancel Link
        │                                                                │
        └──────────────────────────────┬──────────────────────────────┘
        │
        ▼
[30 Minutes Before Scheduled Meeting Time]
        │
        ▼
[PRE-DEMO LEAD-IN EMAIL AUTO-DISPATCHED]
- Prospect's Custom Audit PDF Attached
- Rent Analysis Bot Phone Number Provided
- Action Prompt: "Text any property address to test live"
```

**Self-Serve Audit Landing Page (3.6-Pixel):** High-converting public landing page where PMs can enter their corporate domain to request a certified speed audit. Automatically triggers background ghost-shopper workers, captures inbound lead details, and redirects high-intent prospects to the calendar booking interface. Embedded Meta and Google pixels build qualified retargeting audiences under strict wallet budgets.

**Show-Rate Reminder Chain:** Automated email and transactional SMS confirmations sent immediately upon booking; 24-hour reminder email highlighting agenda and market-specific growth benchmarks; same-morning SMS ping (8:00 AM contact local time) confirming rep availability and providing a one-click rescheduling link.

**No-Show & Reschedule Handler:** If a prospect fails to attend within 10 minutes of scheduled start, rep triggers Mark No-Show in Slack. Automatically pauses outreach sequences and enqueues a multi-channel recovery flow offering friction-free calendar re-booking.

#### 3.1.6 Sales Demo Kit & Rent Analysis Bot Minimum Real Version

```
RENT ANALYSIS BOT (`BOT-MIN`) LIVE DEMO WEAPON

[Prospect Texts Property Address During Live Pitch: "123 Ocean Dr, Tampa FL"]
        │
        ▼
[Twilio Webhook Ingests SMS to /api/v1/rentbot/demo]
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ Dynamic Address Parsing & Normalization Engine               │
└──────────────────────────────┬──────────────────────────────┘
        │
┌──────────────────────┴──────────────────────┐
▼                                                ▼
[Live Real-Estate Data API]              [Demo-Mode Fallback Cache]
- CoreLogic / RentCast Call               - Pre-Computed Metro Valuations
- Response Time: ~15-30s                  - Response Time: <5s (Guaranteed)
        │                                                │
        └──────────────────────┬──────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ Automated SMS Valuation Reply Dispatched in <60 Seconds:      │
│ "123 Ocean Dr, Tampa: Est Rent $2,450/mo (Range $2.3k-$2.6k)  │
│ Confidence Score: 94%. Powered by Blackink Rent Engine."      │
└─────────────────────────────────────────────────────────────┘
```

**Permanent Demo Friday Sandbox Client:** Formally designated, permanent test client environment populated with realistic operational data. Pulls up live Looker dashboards, active mock campaigns, lead-matching logs, and sample performance evidence packets on demand during sales calls.

**Pre-Demo Lead-In Automation:** Fires automatically exactly 30 minutes before any scheduled sales demonstration. Emails the prospect their compiled Speed & Revenue Loss Report, provides the dedicated phone number for the Rent Analysis Bot, and instructs them to text a residential address to test the inbound AI live.

**Rent Analysis Bot Minimum Real Version (BOT-MIN):** A dedicated Twilio phone number running an SMS webhook receiver. Parses inbound property addresses, queries rental valuation APIs, and returns estimated monthly rent, confidence scores, and local rent comps within 60 seconds. **Demo-Mode Fallback Cache:** If an external valuation API times out or fails during a live pitch, the bot detects demo mode and serves an accurate, pre-computed local valuation from cache in under 5 seconds, ensuring the live demo never fails.

#### 3.1.7 Day-One Learning & Pipeline Metrics Engine

Every interaction in Week 1 generates structured entries in the events table:

```json
// Example: Structured Touch Payload logged to events table
{
"event_id": "8f3b2d1e-9a4c-4b5d-8e7f-1a2b3c4d5e6f",
"client_id": "00000000-0000-0000-0000-000000000001",
"company_id": "3c4d5e6f-7a8b-9c0d-1e2f-3a4b5c6d7e8f",
"contact_id": "5e6f7a8b-9c0d-1e2f-3a4b-5c6d7e8f9a0b",
"event_type": "outbound_touch_dispatched",
"source": "cold_outbound_sequencer",
"payload": {
"touch_step": 1,
"channel": "email",
"recipient_email": "j.smith@suncoastpm.com",
"template_version": "v1.4_speed_audit_video",
"ghost_shopper_speed_sec": 15120,
"sendspark_video_id": "spk_99281a",
"sending_domain": "growth-blackink.com",
"mailbox_id": "mbx_04"
},
"occurred_at": "2026-09-02T14:32:10Z"
}
```

**60-Second Post-Meeting Form:** To capture sales conversation outcomes from appointment #1, closers complete a 60-second Slack modal following every completed meeting, capturing Meeting Attendance Status (`Held`, `No-Show`, `Rescheduled`), Target PM Software, Estimated Door Count, Stated Objections (`Pricing`, `Software Integration`, `Capacity`, `Existing Agency`), and Next Action — feeding directly into the Owner Score ranking engine and Prospecting Agent.

**Pipeline Reporting & Slack Metrics Digest (1.6-Pipe):** Real-time Looker Studio dashboards connected directly to PostgreSQL read-replicas, with an automated daily morning Slack digest posted to `#blackink-command` summarizing audits completed, average metro response latency, cold emails dispatched, open/click-through/video completion rates, and appointments booked.

#### 3.1.8 Week 1 Milestone Definition of Done

The Sprint 1 / Marketing Live milestone is officially cleared on September 11, 2026, upon successful execution of the following live demonstration contract:

1. **Live Outbound Dispatch:** Demonstrate automated multi-touch email sequencing dispatching from warmed Google Workspace/Outlook inboxes across dedicated domains with verified SPF/DKIM/DMARC.
2. **End-to-End Ghost-Shopper Audit:** Submit a live test inquiry through a target property management website; capture the response latency in seconds; generate a branded, dynamic Speed & Revenue Loss PDF; and verify that Sendspark personalized video parameters merge cleanly.
3. **Interactive Booking Flow:** Complete a live meeting booking through the Calendly/Google Calendar integration; verify creation of the `meeting_booked` event in PostgreSQL; and confirm immediate delivery of email and transactional SMS confirmations.
4. **Lead-In Automation & Rent Analysis Bot:** Trigger the 30-minute pre-demo lead-in email; text a live Florida residential address to the Rent Analysis Bot phone number; and verify an accurate SMS rental valuation reply in under 60 seconds.
5. **Interactive Slack Hub:** Execute approval, revision, and snooze actions inside `#blackink-setter` and `#sales-replies`, verifying that payload-bound hash security blocks altered payloads.
6. **Zero SMS Outbound to Unconsented Contacts:** Execute the automated CI/CD compliance suite, demonstrating that cold outbound SMS is hard-blocked at the gate and rejected by runtime linters.

### Week 2 (Sept 14 – Sept 18, 2026): Sprint 2A — Settlement, Triage Agent & Founding Client Pilot

**Milestone Standard:** September 16–18 Founding Client Pilot Ready (Automated Inbound Reply Classification, Zero-Deposit Card Authorization & Settlement Rails, Tenant-Isolated Deliverability, and Hand-Assisted Tenant Deployment)

Week 2 builds the core fulfillment, settlement, and service architecture that powers customer monetization and tenant operations. While Week 1 focused on outbound market demand, audits, and sales demos, Week 2 establishes the closed-loop machinery that manages incoming prospect intent, processes performance-based Stripe billing, executes core revenue recipes, and deploys the first founding client tenant.

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
┌──────────────────────────────────────────────────────────────────────────────────────┐
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

The Reply Triage Agent eliminates manual inbox monitoring by classifying inbound communication across all channels (Email, SMS, Website Forms) and executing appropriate workflow actions within seconds.

| Intent Class | Description & Context Patterns                                                                                | Automated Platform Action                                                                             | Routing Destination                    |
| ------------ | ------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------- |
| HOT_LEAD     | Explicit buying interest ("Let's talk", "Call me tomorrow", "How much do you charge?").                       | Halts cold sequence; extracts phone/availability; generates 1-screen context card.                    | #blackink-setter & instant SMS to rep. |
| QUESTION     | Informational queries regarding pricing, contract terms, software compatibility, or service areas.            | Evaluates knowledge base; drafts auto-response if confidence ≥90%, else queues for human review.     | Thread in#sales-replies.               |
| OBJECTION    | Pushback on timing, current satisfaction, pricing, or internal capacity ("We already have an in-house team"). | Pulls objection handling playbook; equips Setter Copilot with counter-arguments.                      | Context card in#blackink-setter.       |
| LATER        | Timing delay with future re-engagement signal ("Reach out in Q1", "Lease expires in December").               | Ingests date into Reactivation & Nurture timing memory; pauses active campaign until target date.     | Reactivation queue.                    |
| NURTURE      | Mild interest without immediate commitment ("Send more info", "Add me to your newsletter").                   | Transitions contact to low-frequency monthly educational nurture sequence.                            | Nurture campaign stream.               |
| UNSUBSCRIBE  | Requests for removal ("Remove me", "Stop emailing", "Take me off your list").                                 | Executes deterministic opt-out; writes is_opted_out=TRUE across entity and domain suppression tables. | Compliance ledger (Zero human touch).  |
| COMPLAINT    | Aggressive or dissatisfied responses regarding outreach frequency or cold contact.                            | Immediately halts sequence; logs complaint in QA monitor; suppresses apex domain globally.            | #blackink-qa.                          |
| LEGAL_GRIEF  | Threats of legal action, TCPA/CAN-SPAM citations, or regulatory complaints.                                   | Hard circuit-breaker trip; freezes all company contacts; alerts executive Slack immediately.          | #blackink-command (Priority P0).       |
| WHALE_OWNER  | Prospect identified as managing or owning a large portfolio (≥50 units or multi-property LLC).               | Enforces VIP routing; triggers instant phone notification to closer; locks high-priority SLA timer.   | Direct closer alert +#blackink-setter. |
| PARTNER      | Inquiries from Realtors, vendors, lenders, or property management service providers.                          | Routes to Referral Agent; categorizes partner type for reciprocal introduction workflows.             | #client-growth (Referral Desk).        |

```sql
-- Schema: src/db/migrations/002_triage_routing.sql
CREATE TABLE inbound_messages (
message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
client_id UUID NOT NULL,
contact_id UUID REFERENCES contacts(contact_id),
channel VARCHAR(20) NOT NULL, -- 'EMAIL', 'SMS', 'WEB_CONCIERGE'
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

**Confidence Thresholding:** If classification confidence is ≥90% and matches an approved knowledge base entry, the system prepares an automated response payload. If confidence is <90% or the intent is classified as `OBJECTION`, `LEGAL_GRIEF`, or `HOT_LEAD`, the message bypasses auto-responders and creates an urgent task in Slack.

**Setter Context Card Generation:** For `HOT_LEAD` and `WHALE_OWNER` classifications, the agent compiles an instant context card into `#blackink-setter` containing prospect full name, verified title, company name, and estimated door count; summary of ghost-shopper speed audit latency and estimated annual revenue loss; Sendspark video watch analytics; complete previous touch history; and suggested opening talk-track with a direct one-click calendar booking link.

**SLA Escalation Timers:** Inbound hot leads trigger strict response timers — 15 minutes: unclaimed hot leads generate a second high-priority ping in Slack; 60 minutes: escalates to executive mobile notification; 240 minutes: automatically reallocates lead to the backup closer queue.

#### 3.2.2 Stripe Card Authorization & Settlement Rails (1.7-Stripe)

Blackink operates on a verified success-only economic model. Clients pay zero upfront fees, zero onboarding retainers, and zero monthly minimums prior to verified results.

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
┌─────────────────────────────────────────────────────────────┐
│ 50/50 Billing Split Engine Executes:                          │
│ 1. Triggers First 50% Charge via ACH (Card Backup)             │
│ 2. Compiles Dynamic Evidence Packet PDF                        │
│ 3. Schedules 60-Day Clawback Verification Job in PostgreSQL    │
└──────────────────────────────┬──────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│ At Day 60: Automated PMS Verification Check Runs               │
├──────────────────────────────┬──────────────────────────────┤
│ [Agreement Active in PMS]     │ [Agreement Cancelled <60d]     │
│ ──► Charge Remaining 50%      │ ──► Auto-Void Back-Half 50%    │
│ ──► Email Receipt + PDF       │ ──► Log Clawback Event to DB   │
└──────────────────────────────┴──────────────────────────────┘
```

**A. Card Authorization & ACH Mandate Capture** — During onboarding, the client submits credit card and bank account details through a secure Stripe Elements modal. A temporary $1 authorization hold verifies card validity and is immediately released. A persistent Stripe `setup_intent` establishes an ACH Direct Debit mandate (`pm_ach_debit_mandate_id`); ACH serves as the primary billing rail for five-figure invoice volumes to eliminate credit card processing fees, with the authorized credit card retained as secondary backup.

**B. The 50/50 Settlement Split Mechanics** — When a management agreement is verified, Blackink invoices the performance bounty or qualified appointment fee across two equal installments: Installment 1 (50% at Signature) charged immediately upon nightly verification; Installment 2 (50% at Day 60) placed into an automated scheduling queue in PostgreSQL.

**C. Automated 60-Day Clawback Trigger** — The verification daemon cross-references active client rent rolls and property management agreements every 24 hours. If a newly signed property is terminated, cancelled, or offboarded within 60 days, the scheduled second 50% installment is automatically voided in Stripe. A `settlement_clawback_executed` event is logged, attaching the PMS cancellation record and notifying Josh and the client via email.

**D. Dynamic Evidence Packet Compiler** — Every charge automatically compiles and attaches a comprehensive PDF Evidence Packet: Section 1 (Source & Outreach Lineage), Section 2 (Engagement & Booking Record), Section 3 (Meeting & Qualification Verification), Section 4 (PMS Contract Verification).

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

Blackink avoids shared From-Name pools. To protect deliverability under performance-based pricing, email and SMS sending infrastructure is strictly isolated at the tenant level.

```
TENANT-ISOLATED SENDING POOL ARCHITECTURE (20 DOMAINS / 40 MAILBOXES)

┌──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┐
│ Blackink Internal Outbound (5 Domains / 10 Mailboxes)     │ Client Outbound Dedicated Pools (15 Domains / 30 Mailboxes)│
├──────────────────────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
│ - growth-blackink.com (2 Mailboxes)                       │ - Client 1 Dedicated Pool: 3 Domains (6 Mailboxes)          │
│ - connect-blackink.com (2 Mailboxes)                      │ - Client 2 Dedicated Pool: 3 Domains (6 Mailboxes)          │
│ - audit-blackink.com (2 Mailboxes)                        │ - Client 3 Dedicated Pool: 3 Domains (6 Mailboxes)          │
│ - pm-blackink.com (2 Mailboxes)                           │ - Client 4 Dedicated Pool: 3 Domains (6 Mailboxes)          │
│ - scale-blackink.com (2 Mailboxes)                        │ - Client 5 Dedicated Pool: 3 Domains (6 Mailboxes)          │
└──────────────────────────────────────────────────────────┴───────────────────────────────────────────────────────────┘
```

**Mailbox Assignment Model:** The 20 pre-purchased domains are divided: 5 domains (10 mailboxes) dedicated exclusively to Blackink's own self-marketing; 15 domains (30 mailboxes) partitioned across client tenants, assigning a dedicated 3-domain / 6-mailbox cluster to each active client. Sending identity and reputation are never pooled across clients.

**Per-Mailbox Pacing & Volume Caps:** Mailboxes strictly enforce a daily ceiling of 30–50 cold emails per day, with automated rotation algorithms distributing sequence steps evenly across the client's 6 assigned mailboxes.

**Deliverability Sentinel & Auto-Quarantine:** Background monitors track bounce rates, spam complaint deltas, and open rate decay per domain. If a client domain records a bounce rate >3% or a spam complaint rate >0.08% within a rolling 48-hour window, the Sentinel trips an automatic quarantine: pauses outbound dispatch, replaces the degraded domain with a pre-warmed reserve domain, and posts an alert to `#blackink-qa`.

#### 3.2.4 Client Core Revenue Recipes Initialization

**A. The Win-Back Recipe (1.8-Rec)** — Ingests the client's historical dead leads, lost owners, and cancelled management agreements. Runs automated skip-tracing and DNC/suppression screening, then dispatches a 3-touch hyper-personalized re-engagement sequence from the client's own domain: Touch 1 (Market Shift Angle), Touch 2 (Ancillary Value / Ryse Angle), Touch 3 (Direct Check-in). Deploys at a new client in under 2 hours, producing booked appointments within the first 72 hours of tenant launch.

**B. The Speed-to-Lead Recipe (1.9-Rec)** — Captures incoming owner inquiries from the client's website, listing portals, and paid campaigns, delivering sub-60-second automated responses and instant calendar booking.

```
SUB-60-SECOND SPEED-TO-LEAD FLOW (DUAL INGESTION PATH)

┌───────────────────────────────────────┐  ┌────────────────────────────────────────┐
│ Path A: Direct Webhook Source          │  │ Path B: Unintegrated Email Notification │
│ (Website Form, Paid Landing Page)      │  │ (Zillow, Trulia, HotPads, MLS Forms)    │
└──────────────────┬────────────────────┘  └───────────────────┬────────────────────┘
        │ Webhook Ingest (<2s)                          │ Inbound Parse Hook (<5s)
        ▼                                                ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Speed-to-Lead Orchestrator (Validates Phone/Email & Checks Non-Poach Gate)                     │
└──────────────────────────────────────────────┬───────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Automated Response Engine (<60 Seconds Total Elapsed Time):                                    │
│ 1. Triggers Immediate Transactional SMS with 10-min call offer                                  │
│ 2. Dispatches Personalized Confirmation Email with Real-Time Booking Calendar Link              │
│ 3. Fires High-Priority Alert to Closer Queue in Slack #blackink-setter                          │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Email-Parsing Fallback:** For legacy listing sources that do not support webhooks, client inquiry notification emails route to a dedicated tenant parse address (`leads@{client-subdomain}.blackink.io`), extracting prospect name, phone, address, and inquiry text via regex and firing the identical sub-60s workflow.

**C. The Async-Close Path (1.10-Async)** — Enables small residential owners (1–2 units) to sign standard management agreements electronically. Inbound leads meeting small-portfolio criteria receive an automated video walkthrough of the management agreement alongside an embedded e-signature link, logging a `door_signed` event upon document completion without consuming closer calendar slots.

#### 3.2.5 Founding Client Pilot Deployment (September 16–18 Milestone)

| Stage                   | Operational Procedure Executed                                                                            | Acceptance Verification Standard                               |
| ----------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| 1. Tenant Cloning       | Clone Golden Client schema; inject tenant identifiers; provision dedicated 3-domain sending cluster.      | Isolated tenant workspace live with zero credential leakage.   |
| 2. Historical Ingest    | Ingest 500+ client dead leads and past owner records via CSV import; execute automated DNC scrub.         | Data normalized, deduped, and suppression flags verified.      |
| 3. Compliance Guard     | Execute cross-client non-poach check; verify opt-out tables; validate state-specific calling hours.       | Zero overlap with existing books; email-first waterfall holds. |
| 4. Campaign Launch      | Arm Win-Back and Speed-to-Lead recipes; dispatch initial batch from client-dedicated mailboxes.           | Live outbound emails delivering; tracking webhooks active.     |
| 5. Triage & Routing     | Ingest live prospect replies; execute automated intent classification; post context cards to Slack.       | Replies classified correctly; hot leads routed to setter <60s. |
| 6. Booking & Dashboards | Complete live meeting booking; verify calendar ICS; update Client Wins Dashboard with real event metrics. | Looker dashboard reflects live appointments and pipeline.      |

**Hands-On Engineering Support:** As established in the Engagement Agreement, engineering assistance is explicitly permitted during the September 16–18 pilot. Clients #1–2 are intentionally managed with hands-on technical guidance to observe friction points, refine database mappings, and harden the operational runbook before enforcing the zero-code standard on September 30.

#### 3.2.6 Week 2 Acceptance Criteria & Definition of Done

The Sprint 2A milestone is officially complete on September 18, 2026, when all of the following technical deliverables are demonstrated live:

1. **Automated Reply Classification:** Process a test batch of 50 multi-channel replies across all 10 intent classes; verify that the Reply Triage Agent achieves ≥80% classification accuracy and correctly routes high-intent leads to Slack with context cards.
2. **Deterministic Settlement Execution:** Execute a test outcome transaction in Stripe; verify zero dollars charged upfront, successful ACH mandate storage, generation of the timestamped Evidence Packet PDF, and creation of the 60-day clawback verification job.
3. **Tenant-Isolated Sending Verification:** Demonstrate that test campaigns dispatched for Tenant A execute exclusively through Tenant A's dedicated domain cluster, with zero From-Name or domain crossover.
4. **Speed-to-Lead Sub-60s Execution:** Trigger a test lead via webhook and via the email-parsing address; verify that transactional SMS response, confirmation email, and closer Slack notifications execute in under 60 seconds.
5. **Operational Pilot Tenant:** Demonstrate a live, functioning founding client tenant executing active win-back sequences, routing replies into Slack, and displaying real-time metrics in the Client Wins Dashboard.

### Week 3 (Sept 21 – Sept 25, 2026): Sprint 2B — 15-State Portal, Cloner Runbook, Demo Weapons & Preflight

**Milestone Standard:** Sprint 2B Production Readiness (Full 15-State Client Onboarding Portal, Client Cloner Runbook Path B, Phase 2A Demo Weapons Full Delivery, 72-Hour Launch Preflight Engine, and Operational Control Surfaces)

Week 3 shifts the Blackink platform from a single-tenant deployment into a standardized, repeatable business-in-a-box, focused on: (1) Client Intake — a branded, tokenized 15-state onboarding portal; (2) Repeatable Tenant Provisioning (Path B) — an operational cloning runbook; (3) Phase 2A Demo Weapons — the full multi-API Rent Analysis Bot, Churn Tripwire, and LLC-to-owner portfolio ingestion; (4) Deterministic Preflight & Dashboards.

```
WEEK 3 COMPLETE ONBOARDING, WEAPONS & GOVERNANCE PIPELINE

[Signed Client Agreement / Contract Closed]
        │
[Automatic Link Generation / Signed URL]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 15-State Client Checklist Portal (`ADD-9-FULL`)                                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ States 1–7: Core Setup (Agreement, Auth, PM Profile, Metro Territory, Calendar Slots, Offer Approved)                   │
│ States 8–11: Holistic Value Intake (Historical Dead Leads, Document Upload, Partner Menu, Growth Elections)             │
│ States 12–15: Technical Launch (Isolated Sending, Lead Routing, Live-Fire Test, Client Preflight Approval)              │
└─────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────────┘
┌──────────────────────┴──────────────────────┐
▼                                                ▼
┌────────────────────────────────────────────────────────┐  ┌───────────────────────────────────────────────────────────┐
│ Path B Client Cloner Runbook (`ADD-1-B`)                 │  │ Phase 2A Demo & Retention Weapons                            │
├────────────────────────────────────────────────────────┤  ├───────────────────────────────────────────────────────────┤
│ 1. Clone Golden Client Database & Schema                 │  │ 1. Full Rent Analysis Bot: Multi-API Valuation Engine        │
│ 2. Inject Tenant Profile & PM Software Credentials       │  │ 2. Churn Tripwire: Listing/Deed/Homestead Book Monitor       │
│ 3. Assign 3-Domain / 6-Mailbox Isolated Cluster          │  │ 3. Owner/Portfolio Feed: Multi-LLC Door Aggregation           │
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

#### 3.3.1 The 15-State Client Onboarding Portal (ADD-9-FULL) & Holistic Intake

The client onboarding interface replaces manual coordination with a live-status web application accessed via cryptographically signed URLs.

```
THE 15-STATE ONBOARDING & HOLISTIC ASSESSMENT FLOW

[1. Agreement Signed] ──► [2. Payment Auth (Card + ACH)] ──► [3. Contacts Entered] ──► [4. PM Profile (`pm_profile`)]
        │
[8. Historical Ingest] ◄── [7. Offer / CTA Approved] ◄── [6. Calendar (25 Slots)] ◄── [5. Metro Territory Set]
        │
        ├──► [9. Complete Document Upload ──► Auto-Generates Instant Revenue-Stack Audit Map]
        ├──► [10. Partner Menu Reviewed ──► Checkbox Enrollments Logged for Ancillary Lines]
        └──► [11. Growth & Value Elections ──► Captures New Fees, Streams & Target Services]
        │
[15. Preflight Green / Live] ◄── [14. Live-Fire Test Passed] ◄── [13. Routing Confirmed] ◄── [12. Compliance / Domains]
```

**Detailed State Specification:**

- **State 1 — Agreement Signed:** Contract execution verified via webhook, storing the countersigned agreement and locking core contractual terms.
- **State 2 — Payment Authorization Complete:** Zero-deposit payment setup verifying credit card authorization and capturing an ACH direct debit mandate via Stripe Elements.
- **State 3 — Primary Client Contacts Entered:** Collects and validates contact details for the Managing Broker/Owner and the Operations/Office Lead.
- **State 4 — Property Management Profile Completed:** Populates the `pm_profile` schema (specialty tags, asset classes, accepted property types, languages, historical performance metrics).
- **State 5 — Metro Territory Established:** Defines the contracted geographic territory via zip code arrays or boundary polygons, setting client acceptance criteria and linking the non-poach suppression perimeter.
- **State 6 — Calendar Connected:** Integrates the client's Google Calendar or Calendly workspace, verifying a minimum of 25 open meeting slots across the initial 60-day window.
- **State 7 — Client Offer & CTA Approved:** Confirms standard messaging hooks, switching incentives, and target customer profiles.
- **State 8 — Full Historical Data Ingest (The Win-Back Goldmine):** Direct portal upload of historical CRM exports, past owner lists, cancelled management agreements, and dead leads, normalized and screened against DNC registries to seed the Win-Back recipe.
- **State 9 — Complete Document Upload & Instant Revenue Audit:** Client uploads their standard management agreement, master fee schedule, and operational addenda, triggering the ADD-8-Lite templating pipeline to generate an immediate, branded Revenue-Stack Audit.
- **State 10 — Partner Menu Selections:** Interactive interface presenting pre-negotiated partner lines (pet screening, resident benefits, utility concierge, filter delivery, deposit alternatives, maintenance markup policies, eviction protection, Ryse rent advance), establishing the 35% ancillary revenue tracking baseline.
- **State 11 — Growth & Value Elections:** Structured intake capturing the client's strategic growth goals beyond door count, feeding the 90-day audit cycle and shaping the First-14-Days growth plan.
- **State 12 — Sending Identity & Compliance Approved:** Assigns dedicated sending domains and numbers; generates bidirectional non-poach suppression lists joining the client's current book into the global platform gate.
- **State 13 — Campaign & Lead Routing Confirmed:** Configures inbound lead-capture webhooks and dedicated email-parsing addresses (`leads@{client-subdomain}.blackink.io`).
- **State 14 — Live-Fire Test Passed:** Automated end-to-end test verifying that an injected synthetic lead triggers sub-60s notification, schedules a calendar event, and alerts the closer queue in Slack.
- **State 15 — Preflight Green & Launch Approved:** The preflight validation engine evaluates all prerequisites; once confirmed green, the client clicks "Approve Launch," arming live outbound campaigns.

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
category VARCHAR(100) NOT NULL, -- 'FEE_MODIFICATION', 'NEW_ANCILLARY', 'SERVICE_EXPANSION'
item_name VARCHAR(255) NOT NULL,
selection_status VARCHAR(20) NOT NULL, -- 'YES', 'NO', 'INTERESTED'
modeled_annual_value_cents BIGINT DEFAULT 0,
created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
```

**Auto-Chase Suppression Logic:** As the client completes each state in the portal, an event is logged to the shared ledger. The Launch Agent detects completion events in real time and automatically cancels corresponding chase notifications, preventing redundant follow-up emails.

#### 3.3.2 Client Cloner & Launch OS (Path B Runbook Architecture)

Following the Path B execution model, multi-tenancy is managed via a documented, hardened cloning runbook for Clients #1–2, establishing the blueprint for the automated in-app cloner wizard.

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
│ - Provision Dedicated Inbound Twilio Number   │
└──────────────────────┬───────────────────────┘
        ▼
┌──────────────────────────────────────────────┐
│ Step 3: Non-Poach & Compliance Provisioning   │
│ - Ingest Client Book into Suppression Engine  │
│ - Generate Bidirectional Cross-Client Gate    │
│ - Apply Target Metro Polygon Boundary         │
└──────────────────────┬───────────────────────┘
        ▼
┌──────────────────────────────────────────────┐
│ Step 4: First-14-Days Plan Auto-Generation    │
│ - Parse States 8–11 Intake Payloads           │
│ - Assemble Engine Activation Schedule         │
│ - Render Plan into Client Wins Dashboard      │
└──────────────────────────────────────────────┘
```

1. **Database & Tenant Keying:** Operator runs `clone_tenant_environment.sh` passing the client's legal entity name and primary domain. The script provisions a new tenant profile, binds cryptographic API tokens, and applies row-level isolation rules.
2. **Dedicated Deliverability Cluster Allocation:** Allocates a dedicated cluster of 3 sending domains and 6 Google Workspace mailboxes from the pre-warmed pool; provisions a dedicated Twilio 10DLC local phone number mapped to the client's operating metro.
3. **Suppression & Compliance Initializer:** Ingests the client's current owner roster into the non-poach database, establishing cross-suppression rules that work in both directions.
4. **First-14-Days Plan Generator:** Reviews the client's historical dead-lead count, target metro size, and fee schedule to generate an operational roadmap — Days 1–3: Win-Back activation + Speed-to-Lead routing; Days 4–7: Churn Tripwire activation + initial outbound launch; Days 8–14: Ancillary Partner Menu integration + first weekly performance review.
5. **Rollback & Circuit Breaker Engine:** A documented rollback runbook (`rollback_tenant_provisioning.sh`) revokes API keys, freezes sending queues, decouples suppression joins, and restores the database to its pre-clone state if a configuration error occurs.

#### 3.3.3 Phase 2A Demo & Retention Weapons (Full Engine Delivery)

```
CHURN TRIPWIRE RETENTION MONITORING ENGINE (`2A-TRIPWIRE`)

[Active Client PM Book Ingested & Synced]
        ▼
┌───────────────────────────────────────┐
│ Nightly Public Records & Market Sweep  │
└───────────────────┬───────────────────┘
┌──────────────────────────────────────────────┼──────────────────────────────────────────────┐
▼                                                ▼                                                ▼
[MLS / Public Listing Detector]      [Deed Transfer & Sale Monitor]      [County Tax Record Auditor]
- Scans MLS for Active For-Sale Listings  - Ingests County Clerk Deed Recordings  - Detects Homestead Exemption Drops
- Scans Zillow/Redfin For-Sale-By-Owner   - Identifies Title Transfers / Arm's-Length  - Detects Mailing Address Divergence
- Detects Price Cuts & Status Changes     - Detects Pre-Foreclosure / Lis Pendens  - Identifies Out-of-State Relocations
└──────────────────────────────────────────────┼──────────────────────────────────────────────┘
        ▼
┌───────────────────────────────────────────────────────────────┐
│ Correlation Engine (Matches Properties to Client Owner Book)    │
└───────────────────────────────┬───────────────────────────────┘
        ▼
┌───────────────────────────────────────────────────────────────┐
│ 1. Generates Real-Time Retention Risk Alert in Slack             │
│ 2. Assembles Owner Save Dossier (Property, Signal, Next Step)    │
│ 3. Logs `door_saved` Opportunity to Events Ledger                │
└───────────────────────────────────────────────────────────────┘
```

**A. Full Rent Analysis Bot (2A-BOT-FULL)** — Multi-Source Real-Time Valuation Pipeline integrating live valuation data feeds (RentCast and CoreLogic) via parallel REST API connectors. Confidence Scoring & Range Math evaluates comparable rental properties within a 1.5-mile radius:

> Confidence Score = min(100, (Active Comps Count ÷ 10 × 40) + (1 − σrent ÷ μrent) × 60)

Branded PDF Valuation Tear-Sheet: automated headless worker compiles a 1-page property rent report. Twilio Webhook Controller ingests SMS property queries, executes address standardization, queries valuation APIs, and dispatches SMS replies in under 60 seconds.

**B. Churn Tripwire (2A-TRIPWIRE)** — Continuously cross-references every property address and owner entity in the client's active management database against daily county public records and market signals. Signal Detection Classes: MLS Listing Filings, Deed Transfers & Title Changes, Tax & Homestead Exemption Drops, Out-of-State Mailing Address Changes, Management Agreement Anniversary Flags. When a retention risk is resolved and the owner signs a renewal, the system logs a `door_saved` event to the ledger.

**C. Owner & Portfolio Ingestion Engine (2A-FEED-MIN)** — LLC Entity Resolution ingestion pipeline processes purchased portfolio datasets, linking business entities to parent LLCs and individual managing members. Door Aggregation calculates total residential door counts per owner across Florida counties, identifying high-value targets with 5+ units.

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

| Gate ID | Prerequisite Validated                | Acceptance Standard                   |
| ------- | ------------------------------------- | ------------------------------------- |
| GATE-01 | Stripe Authorization & ACH Mandate    | setup_intent status == 'succeeded'    |
| GATE-02 | PM Profile Schema Completeness        | All required fields populated         |
| GATE-03 | Territory Definition & Polygon Bounds | ≥1 valid ZIP code assigned           |
| GATE-04 | Calendar Meeting Availability         | ≥25 open slots in next 60 days       |
| GATE-05 | Historical Data Ingestion             | ≥50 historical records normalized    |
| GATE-06 | Document Ingestion & Audit Generation | Fee schedule uploaded; audit compiled |
| GATE-07 | Partner Menu Selections               | All partner lines reviewed            |
| GATE-08 | Dedicated Sending Domain Verification | SPF, DKIM, DMARC 100% valid           |
| GATE-09 | Bidirectional Non-Poach Suppression   | Client book cross-indexed in gate     |
| GATE-10 | Live-Fire Roundtrip Verification      | Synthetic lead executes in <60s       |

**OVERALL STATUS: GREEN (ALL 10 GATES PASS)** Enables "Arm Campaigns" control in UI

#### 3.3.5 Operational Dashboards & Control Surfaces

**A. Client Wins Dashboard (DASH-WINS):** Accessible via secure signed URLs, displaying total attended discovery appointments held, verified signed management agreements with door counts, total saved doors identified by the Churn Tripwire, modeled and collected ancillary revenue, and downloadable Evidence Packet PDFs.

**B. Internal Client Control Center (OPS-CENTER):** Single administrative screen providing visibility across all client tenants — live status of the 15 onboarding states per client, real-time deliverability health, pipeline volume metrics, and an open exception queue.

```
INTERNAL CLIENT CONTROL CENTER (`OPS-CENTER`) WIREFRAME

┌───────────────────┬──────────────┬───────────────┬──────────────────┬─────────────────┬────────────────┬─────────────┐
│ Tenant Name        │ Launch State │ Preflight      │ Domain Health     │ Active Leads     │ Booked / Held   │ Exceptions   │
├───────────────────┼──────────────┼───────────────┼──────────────────┼─────────────────┼────────────────┼─────────────┤
│ Suncoast PM        │ State 15/15  │ GREEN          │ 100% (6/6 Warm)   │ 412 In-Flight    │ 14 Booked / 11  │ 0 Open       │
│ Gulf Coast Living  │ State 12/15  │ RED (Gate 04)  │ 100% (6/6 Warm)   │ 0 (Pre-Launch)   │ 0 Booked / 0    │ 1 Blocker    │
│ Tampa Bay Rentals  │ State 15/15  │ GREEN          │ 98% (Spam: 0.02%) │ 680 In-Flight    │ 22 Booked / 19  │ 0 Open       │
│ Orlando Alliance   │ State 08/15  │ RED (Gate 05)  │ Provisioning      │ 0 (Pre-Launch)   │ 0 Booked / 0    │ 1 Blocker    │
└───────────────────┴──────────────┴───────────────┴──────────────────┴─────────────────┴────────────────┴─────────────┘
```

#### 3.3.6 Week 3 Acceptance Criteria & Definition of Done

The Sprint 2B milestone is officially complete on September 25, 2026, when all of the following technical deliverables are demonstrated live:

1. **Complete 15-State Onboarding Demonstration:** Walk through the onboarding portal using a tokenized link; upload sample management agreements and dead leads; select Partner Menu items; confirm growth elections; and verify that completion events suppress chase notifications.
2. **Instant Revenue Audit Generation:** Upload a sample fee schedule in State 9; verify that the engine generates a branded Revenue-Stack Audit PDF displaying fee gaps and partner revenue projections.
3. **Path B Cloner Execution:** Execute the manual cloning runbook on a test tenant, verifying database creation, tenant parameter injection, isolated domain assignment, and suppression indexing.
4. **Full Rent Analysis Bot Live Test:** Send property queries to the bot; verify that the system returns rental valuations, confidence scores, and comparable properties in under 60 seconds.
5. **Churn Tripwire Alert Verification:** Inject a synthetic deed transfer and MLS listing event matching a client property; verify that the system detects the match, posts a retention alert to Slack, and creates a save opportunity in the ledger.
6. **Preflight Deterministic Gating:** Demonstrate that the Preflight engine blocks campaign arming when a prerequisite is missing, and unlocks campaign arming only when all 10 gates evaluate green.
7. **Control Surfaces Operational:** Verify that the Client Wins Dashboard reflects real-time metrics and that the Internal Control Center accurately displays tenant health, onboarding progress, and exceptions.

### Week 4 (Sept 28 – Sept 30, 2026): Sprint 3 — Verification Engine, Retention Suite, Expansion Registry & Zero-Code Acceptance

**Milestone Standard:** September 30 Blackink Business-in-a-Box Production Complete (4-Rule Attended Appointment Verification Bar, Automated Dispute Adjudication, Retention Suite, Expansion Offer Registry, Security Baseline, and Zero-Code Repeatable Tenant Deployment)

Week 4 represents the final production capstone of the September platform build, hardening the operational, verification, and compounding layers of the business across five pillars: the 4-Rule Verification & Dispute Engine; the Phase 3 Retention Suite; the Expansion Registry & Sub-Engines; the Production Security Baseline (SEC-BASE); and the September 30 Production Acceptance Test.

```
WEEK 4 COMPLETE VERIFICATION, RETENTION & ACCEPTANCE ARCHITECTURE

┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ The 4-Rule Attended Appointment Verification Engine & Dispute Adjudication                                             │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Rule 1: Assessor Parcel ID Verified in Polygon        │ Rule 2: Live Attended ≥12 Mins (`proof_ref` Log)                │
│ Rule 3: Pre-Qualification Documented on Row           │ Rule 4: Matches Client Initialed ICP (Exhibit A)                │
│ ──► QA Watchdog Auto-Adjudicates Disputes (Duration + Transcript Ownership Language Check ──► Auto-Deny / Escalate)    │
└──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┘
┌───────────────────────┴───────────────────────┐
▼                                                  ▼
┌────────────────────────────────────────────────────────┐  ┌───────────────────────────────────────────────────────────┐
│ Phase 3 Retention Suite (Pulled Forward to September)    │  │ Expansion Engine & Compounding Sub-Engines                    │
├────────────────────────────────────────────────────────┤  ├───────────────────────────────────────────────────────────┤
│ 1. Retention Guard ($497/mo Book Churn Monitor)          │  │ 1. 17-Row Offer Registry (`entitlement_offers` Table)          │
│ 2. Rent Gap Report ($197/mo Under-Market Engine)         │  │ 2. One-Click In-App Activation (Flips Entitlement Row)         │
│ 3. Owner Report Card ($197/mo White-Label PDF)           │  │ 3. Close Detection (Forwarded PM Email Parser)                 │
│ 4. Anniv. Flags & Deed/Listing/Homestead Watch           │  │ 4. Dead-Book Engine, 4-Channel Referrals, Second Pass          │
└──────────────────────────┬─────────────────────────────┘  └─────────────────────────────┬─────────────────────────────┘
        └───────────────────────────────┬─────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Production Security Baseline (`SEC-BASE`) & Governance Ledgers                                                         │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ - Adversarial Cross-Tenant Leakage Test Suite          │ - Automated Daily PostgreSQL Backups + Verified Live Restore   │
│ - Consent & Channel Eligibility Ledger                 │ - Per-Client Variable Cost Ledger (Enrichment, Twilio, AI, Media)│
│ - Marketer Content Admin Surface (Safe Staging)        │ - Wallet-Capped Paid Growth Modules (Google Search, Meta Harness)│
└──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ SEPTEMBER 30 ACCEPTANCE TEST: BUSINESS-IN-A-BOX ZERO-CODE REPEATABILITY                                                │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Golden Client Run ──► Repeat on Tenant #2 via Written Runbook ──► ZERO Code Touched = PRODUCTION COMPLETE              │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 3.4.1 The 4-Rule Attended Appointment Verification Engine & Dispute Adjudication

Under Blackink's performance-based billing model, revenue is recognized on verified attended appointments. To prevent attribution disputes, every appointment is evaluated programmatically against a 4-rule qualification bar before an invoice is issued.

```
THE 4-RULE PROGRAMMATIC VERIFICATION BAR

[Appointment Completed on Closer / Client Calendar]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ RULE 1: Property Ownership & Parcel Verification                                                    │
│ Assessor Parcel ID (APID) for ≥1 residential unit in contracted polygon verified in database.       │
└─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
        │ [PASSED]
┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│ RULE 2: Minimum Attended Duration (≥12 Minutes)                                                      │
│ Both parties present on calendar event for ≥12 minutes; call recording or timestamp stored.          │
└─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
        │ [PASSED]
┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│ RULE 3: Documented Pre-Qualification Record                                                          │
│ Prospect's explicit inbound SMS, email reply, or audit form answer stored on outcome row.            │
└─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
        │ [PASSED]
┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│ RULE 4: Ideal Customer Profile (ICP) Compliance                                                      │
│ Unit count, property type, and asset class fall within client's initialed Exhibit A agreement.       │
└─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
        │ [ALL 4 PASS]
┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
│ STATUS: BILLABLE ATTENDED OUTCOME                                                                    │
│ - Stripe ACH Direct Debit drafted next business day                                                  │
│ - Evidence Packet PDF auto-compiled and emailed with receipt                                         │
│ - Settlement row updated with timestamp and transaction ID                                            │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Verification Criteria Specification:**

- **Rule 1 (Assessor Parcel ID Verification):** The booking payload must resolve to a verified residential property within the client's contracted geographic territory; the county tax assessor parcel ID (APID) is validated against public property feeds before billing is initiated.
- **Rule 2 (Live Attendance Duration ≥12 Minutes):** Both the property owner and the client representative must remain on the calendar bridge for a minimum of 12 verified minutes, stored in the database as `proof_ref`.
- **Rule 3 (Documented Pre-Qualification):** The outcome row must contain the prospect's explicit pre-qualification statement captured via SMS, email, or inbound intake prior to the calendar hold.
- **Rule 4 (ICP Alignment per Exhibit A):** The opportunity must match the property management firm's contracted ICP criteria (residential single-family/small multifamily, minimum rent thresholds, valid geographic boundary).

**Failure Allocation Rule:** If Rule 1, 3, or 4 fails, Blackink absorbs the cost and no charge is generated. A replacement credit is issued only if Rule 2 fails due to a legitimate, documented prospect no-show or early disconnect.

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
icp_criteria_passed BOOLEAN DEFAULT TRUE,
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

**Dispute Adjudication Workflow:** Clients have a 5-business-day window to flag an appointment outcome. The QA Watchdog Agent auto-adjudicates claims: (1) Duration Verification — checks `proof_ref` duration log; (2) Transcript Linguistic Evaluation — evaluates whether property ownership, unit counts, or management needs were discussed; (3) Deterministic Verdict — if duration was ≥12 minutes AND transcript contains verified ownership context, the dispute is Auto-Denied with an evidence summary; if duration was <12 minutes due to a legitimate prospect departure, a replacement credit is issued automatically (1 free replacement per 4 billables, max 4/month).

**Retention Floor Rule:** If rolling 90-day signed-to-attended conversion is ≥15%, no goodwill credits are owed. If conversion drops below 8% for two consecutive months, either party may terminate without penalty.

#### 3.4.2 Phase 3 Retention Suite (Pulled Forward to September)

Acquiring a new property management client costs substantial capital, but firms lose 15–25% of their doors annually to preventable owner churn. Week 4 deploys three automated recurring products pointed inward at clients' existing portfolios.

```
PHASE 3 RETENTION SUITE ARCHITECTURE

[Client's Active Managed Doors Synchronized]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Retention Guard Engine ($497/month Subscription)                                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Scans client's active owner book nightly across 5 risk vectors:                                                         │
│ 1. MLS & FSBO Sale Listings: Detects for-sale listings before the manager is notified                                    │
│ 2. County Deed Transfers: Flags quitclaims, warranty deeds, or title transfers                                           │
│ 3. Homestead Exemption Filings: Identifies rentals transitioning to primary residences                                   │
│ 4. Out-of-State Tax Address Changes: Flags relocation and estate transitions                                             │
│ 5. Agreement Anniversary Triggers: Prepares renewal campaigns 60 days prior to expiry                                    │
└─────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────────┘
┌──────────────────────┴──────────────────────┐
▼                                                ▼
┌────────────────────────────────────────────────────────┐  ┌───────────────────────────────────────────────────────────┐
│ Rent Gap Report ($197/month Engine)                       │  │ Owner Report Card ($197/month Retention Product)              │
├────────────────────────────────────────────────────────┤  ├───────────────────────────────────────────────────────────┤
│ - Analyzes rent rolls against current market comps        │  │ - Automated quarterly branded PDF generated per owner          │
│ - Identifies units renting ≥10% under market               │  │ - Displays property equity gains, local market yield,           │
│ - Arms manager with rent increase justification data       │  │   maintenance summaries, and portfolio performance metrics      │
│ - Drives client management fee growth & owner loyalty       │  │ - Makes independent PMs look institutional to owners            │
└────────────────────────────────────────────────────────┘  └───────────────────────────────────────────────────────────┘
```

**Retention Guard (`retention_guard` — $497/mo):** Executes nightly public records and market scans across every property in the client's database, flagging sell signals, price drops, and listing filings before the property leaves the book. Preserving 20 doors annually on a 400-door portfolio saves ~$43,000 in management revenue, delivering a ~7x ROI.

**Rent Gap Report (`rent_gap_report` — $197/mo):** Ingests active lease rates and compares them against real-time hyper-local market comps, compiling a rent-increase brief that proportionally increases the manager's 8–10% management fee.

**Owner Report Card (`owner_report_card` — $197/mo):** Generates white-labeled quarterly property performance reports for each property owner, serving as a low-cost, institutional retention tool.

#### 3.4.3 Expansion Engine, Offer Registry & Sub-Engines

Blackink enforces a strict architectural invariant: every commercial product, tier, add-on, seat, and fee exists as a database row in the `entitlement_offers` table. No pricing logic is compiled into agents.

```sql
-- Schema: src/db/migrations/007_expansion_registry.sql
CREATE TABLE entitlement_offers (
offer_id VARCHAR(100) PRIMARY KEY,
display_name VARCHAR(255) NOT NULL,
price_cents BIGINT NOT NULL,
billing_model VARCHAR(50) NOT NULL, -- 'FREE', 'SUBSCRIPTION_MONTHLY', 'METERED_EVENT', 'UPFRONT_PACK', 'REVENUE_SHARE'
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

**The 17 Day-One Offer Rows (Seeded in PostgreSQL):**

| Row ID                 | Price / Model       | Trigger Condition                     | Eligibility Predicate                   |
| ---------------------- | ------------------- | ------------------------------------- | --------------------------------------- |
| county_rank            | $0 (Free)           | Firm appears in sweep                 | Always eligible                         |
| first_appointment_free | $0 (Free)           | 30+ days on free tier                 | 1 per firm lifetime; max 5/county/mo    |
| appt_owner_1_4         | $175 / event        | Attended meeting (1–4 doors)         | Passes 4-rule verification bar          |
| appt_owner_5_9         | $350 / event        | Attended meeting (5–9 doors)         | Passes 4-rule verification bar          |
| appt_owner_10plus      | $500 / event        | Attended meeting (10+ doors)          | Passes 4-rule verification bar          |
| appt_seat_a_rate       | $125 / event        | Attended meeting (Seat A holder)      | Client holds active Seat A              |
| appt_pack_8            | $1,200 pack         | Upfront purchase                      | Account active; drawn down per show     |
| appt_standing_order    | $320 / month        | Pack drawn down in <3 weeks           | Subscription; includes 2 appts/month    |
| appt_principal         | $750 / event        | Attended meeting (20–80 doors)       | Passes 4-rule bar; principal verified   |
| appt_second_pass       | $75 / event         | Pre-qualified lead ≥90d prior        | Transferable consent verified in DB     |
| appt_deadbook          | $75 / event         | Sourced from client dead-book         | Passes 4-rule bar; dead-book source     |
| respond                | $249 / month        | Inbound response tier                 | Inbound concierge active                |
| retention_guard        | $497 / month        | Client book churn monitoring          | Client PM book uploaded                 |
| growth_os              | $897 / $997 mo      | Door Growth Operating System          | All county engines active               |
| large_book_band        | $1.50–$2.50/door   | Client book ≥400 doors               | Replaces Growth OS; metered monthly     |
| rent_gap_report        | $197 / month        | ≥5 units renting under market        | Rent roll uploaded                      |
| owner_report_card      | $197 / month        | Client active ≥60 days               | Quarterly reporting enabled             |
| deadbook_engine        | $99/mo + $75/appt   | Free pass produced ≥1 show           | Dead-book records available             |
| county_additional      | $397 / month        | ≥10 doors in adjacent county         | Target county active                    |
| vendor_intro           | $50 / door          | Partner Menu lines active             | Earned in 4 tranches ($20/$10/$10/$10)  |
| seat_a_door_gen        | $999 → $1,999/mo   | First refusal on county leads         | 1 per county; escalates at 26 appts/60d |
| seat_b_comp_intel      | $999 / month        | Competitor distress watch             | 1 per county; displacement active       |
| seat_both              | $1,799 → $2,799/mo | Dual county exclusivity               | Combines Seat A and Seat B              |
| seat_adjacent          | $799 / month        | Right of first refusal on next county | Adjacent market opening                 |

**Compounding Sub-Engines:**

- **One-Click In-App Activation:** When a client approves an expansion recommendation, the system executes an atomic transaction flipping the entitlement row in PostgreSQL and updating Stripe billing.
- **Close Detection Email Parser:** Ingests forwarded confirmation emails from client PM software, extracts owner and property data via regex, and sets `signed=TRUE` on the corresponding outcome row automatically.
- **Dead-Book Reactivation Engine (`deadbook_engine`):** Connects to client historical dead-lead lists, providing free initial ingestion and running compliant outreach from the client's own domain, billed at $99/month plus $75 per attended appointment.
- **4-Channel Referral Credit Ledger:** Client-to-Client Referral ($250 credit at first paid month); New County Referral ($500 credit); Free-Tier Referral (1 free appointment credit); Vendor Introductions (reciprocal revenue-share credits); Ask Trigger fires automatically upon `signed=TRUE` detection.
- **Second Pass & Unsold Routing:** Where an attended appointment does not convert within 90 days, the pre-qualified owner routes into the Second Pass pool ($75/appointment) with transferability consent verified, unless still in the original client's active suppression file.
- **Two-Seat County Exclusivity & First-Refusal Timers:** Supports `seat_a_holder` (exclusive door generation) and `seat_b_holder` (competitor intelligence) per county. County appointments are held exclusively for the Seat A holder until 9:00 AM the next business day. If Seat A and Seat B belong to competing firms, Seat B displacement campaigns automatically suppress Seat A's active client book.

#### 3.4.4 Production Security Baseline (SEC-BASE) & Governance Ledgers

```
SECURITY, ISOLATION & GOVERNANCE ARCHITECTURE

┌───────────────────────────────────────┐  ┌────────────────────────────────────────┐
│ Consent & Channel Eligibility Ledger    │  │ Per-Client Cost & Margin Ledger          │
│ (`CONSENT-LEDGER`)                      │  │ (`COST-LEDGER`)                          │
├───────────────────────────────────────┤  ├────────────────────────────────────────┤
│ - Durable per-contact audit trail       │  │ - Variable cost tracking per tenant:      │
│ - Exact consent capture timestamps      │  │   Enrichment, Twilio SMS/Voice,           │
│ - Source list attribution & channel     │  │   Instantly, AI tokens, Paid spend        │
│ - Verified opt-out expiration dates     │  │ - True contribution margin calculated     │
└───────────────────┬───────────────────┘  └───────────────────┬────────────────────┘
        └──────────────────────────┬───────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Production Security Baseline (`SEC-BASE`) Infrastructure                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Automated Cross-Tenant Leakage Test Suite (Validates zero cross-client record queries)         │
│ 2. Role-Based Access Control (RBAC) & Encrypted Secrets Storage                                   │
│ 3. Automated Daily PostgreSQL Backups with Demonstrated Live Restore Test in Staging               │
│ 4. Marketer Content Admin Surface: Safe template staging & non-technical copy management           │
│ 5. Wallet-Capped Paid Growth: Hard budget limits ($150–$300 initial risk) on Google & Meta          │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**A. Cross-Tenant Leakage Test Suite:** An automated adversarial test suite executes nightly in CI/CD, injecting synthetic tenant records across multiple counties and simulating cross-tenant database lookups, vector retrievals, and campaign dispatches. Asserts that database queries partition strictly by `client_id`, failing the build if any cross-client data leakage is detected.

**B. Automated Backups & Disaster Recovery Verification:** Configures automated daily PostgreSQL snapshots with point-in-time recovery (PITR). A full database restore is executed in an isolated staging environment, demonstrating recovery of all tables, indexes, and event streams without data loss.

**C. Governance & Cost Ledgers:** Consent & Channel Eligibility Ledger (`CONSENT-LEDGER`) is an immutable datastore for compliance inquiries; Per-Client Cost Ledger (`COST-LEDGER`) ingests variable operational costs to render gross revenue alongside net contribution margin; Marketer Content Admin Surface (`CONTENT-ADMIN`) allows non-technical marketing personnel to update copy safely; Wallet-Capped Paid Growth configures Google Search and retargeting sync under strict database wallet caps ($150–$300 initial risk budget per client).

#### 3.4.5 The September 30 Production Complete Acceptance Test

The ultimate definition of done is the September 30 Business-in-a-Box Repeatability Test. The entire platform lifecycle must execute across two distinct environments with zero code modifications.

| Stage                          | Operational Procedure Executed                                                                                     | Acceptance Standard                                                      |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| 1. Golden Client Demonstration | Run complete lifecycle: Clone → Onboard → Launch → Service → Measure → Bill with Evidence Packet.             | Demonstrated live on video call; all event records valid in DB.          |
| 2. Second Tenant Repeat Test   | Execute Path B Cloner Runbook on a fresh test tenant; import dead leads, verify preflight, launch, and bill.       | Completed by an operator following written runbook with ZERO code edits. |
| 3. Verification & Settlement   | Inject attended appointment; verify 4-rule evaluation, Stripe payment intent, and dynamic Evidence PDF generation. | Settlement row created; ACH drafted; evidence PDF attached.              |
| 4. Auto-Dispute Adjudication   | Submit test dispute; verify QA Watchdog evaluates duration and transcript, auto-denying or crediting correctly.    | Verdict rendered programmatically in <30 seconds.                        |
| 5. Retention Guard Alert       | Trigger synthetic deed transfer on client book; verify Retention Guard detects risk and logs save opportunity.     | Alert rendered in Slack; door_saved logged to ledger.                    |
| 6. Security & Restore QA       | Execute CI/CD cross-tenant leakage test suite and show successful database restore in staging environment.         | All assertion tests pass green; zero data leakage verified.              |

**FINAL VERDICT: PRODUCTION COMPLETE — Full 600-Hour Platform Operational**

#### 3.4.6 Week 4 Acceptance Criteria & Final Definition of Done

The Blackink platform achieves final production completion on September 30, 2026, when all of the following deliverables are demonstrated live:

1. **4-Rule Verification & Dispute Adjudication:** Demonstrate automated qualification of an attended appointment against parcel validation, duration logs (≥12 mins), pre-qualification records, and ICP criteria. Submit a synthetic dispute and confirm the QA Watchdog Agent renders a deterministic verdict.
2. **Phase 3 Retention Suite Live:** Demonstrate that Retention Guard detects listing and deed changes, Rent Gap Report identifies under-market units, and Owner Report Card compiles a branded quarterly PDF.
3. **Expansion Offer Registry:** Demonstrate one-click in-app activation of an add-on offer from the 17-row database registry, confirming atomic entitlement updates and Stripe subscription changes.
4. **Sub-Engines Operational:** Verify that the close-detection email parser marks `signed=TRUE`, dead-book reactivation runs on client domains, 4-channel referral credits update the ledger, Second Pass routes 90-day unclosed leads, and Seat A holds leads until 9:00 AM the next business day.
5. **Security Baseline & Live Restore:** Verify passing test results on the cross-tenant leakage test suite, confirm Consent and Cost Ledger records, and demonstrate a successful database backup and live restore in staging.
6. **Two-Tenant Zero-Code Repeatability:** Successfully execute the full business-in-a-box lifecycle on the Golden Client, followed by an immediate, clean execution on a second test tenant via the written runbook without touching a single line of application code.

## 4. Commercial Offer & Database Row Matrix

All commercial terms exist strictly as database rows in the offer registry with Stripe Price IDs.

| Row Key                | Display Name                  | Commercial Model | Trigger & Verification Rule                                         |
| ---------------------- | ----------------------------- | ---------------- | ------------------------------------------------------------------- |
| county_rank            | Speed & Market Rank Report    | Free             | PM appears in county sweep; monthly report emailed.                 |
| first_appointment_free | First Attended Meeting Credit | Free             | 30+ days on free tier (1/firm lifetime, 5/county/mo max).           |
| appt_owner_1_4         | Attended Appt (1–4 Doors)    | Metered Event    | Verified meeting all 4 verification rules (1–4 doors).             |
| appt_owner_5_9         | Attended Appt (5–9 Doors)    | Metered Event    | Verified meeting all 4 verification rules (5–9 doors).             |
| appt_owner_10plus      | Attended Appt (10+ Doors)     | Metered Event    | Verified meeting all 4 verification rules (10+ doors).              |
| appt_seat_a_rate       | Seat A Exclusive Appt Rate    | Metered Event    | Discounted rate for county Seat A holder.                           |
| appt_pack_8            | 8-Appt Bulk Drawdown Pack     | Upfront Pack     | Upfront purchase; drawn down per verified attended meeting.         |
| appt_standing_order    | Monthly Appt Standing Order   | Subscription     | Includes 2 attended appointments/month.                             |
| appt_principal         | Large Operator / Principal    | Metered Event    | Attended meeting with 20–80 door portfolio operator.               |
| appt_second_pass       | Second Pass Re-Offer Lead     | Metered Event    | Pre-qualified owner ≥90 days prior, transferable consent verified. |
| appt_deadbook          | Dead-Book Reactivated Appt    | Metered Event    | Sourced from client historical dead-book ingest.                    |
| respond                | Inbound Speed-to-Lead Tier    | Subscription     | Lead Agent answering inquiries <5m 24/7 (3 engines).                |
| retention_guard        | Client Book Churn Monitor     | Subscription     | Nightly sweep on client's own owners for listing/deed changes.      |
| growth_os              | Door Growth Operating System  | Subscription     | All county engines, referral desk, mail files, owner reporting.     |
| large_book_band        | Scaled Enterprise Tier        | Subscription     | Metered monthly by active door count (≥400 doors).                 |
| rent_gap_report        | Under-Market Rent Report      | Subscription     | Below-market engine identifies ≥5 under-rented units.              |
| owner_report_card      | Quarterly Owner PDF Report    | Subscription     | Client active 60+ days; white-label retention reports.              |
| deadbook_engine        | Dead-Book Campaign Suite      | Sub + Event      | Ongoing dead-book monitoring on client domains.                     |
| seat_a_door_gen        | Exclusive Door Gen Seat       | Subscription     | First refusal on county leads held to 9 AM next business day.       |
| seat_b_comp_intel      | Competitor Intelligence Seat  | Subscription     | Competitor distress watch and displacement campaigns.               |
| seat_both              | Combined Dual-Seat License    | Subscription     | Complete county exclusivity across Seats A and B.                   |
| vendor_intro           | Partner Menu Monetization     | Revenue Share    | Earned in 4 tranches: billable, 90d, 180d, 365d.                    |

## 5. Blackink Governed Nine-Agent Workforce Architecture

**System Specification: Shared Agent Core, Specialist Agent Profiles & Autonomous Operational Framework**

### 5.1 The Agent Operating System (AOS) & Shared Core Architecture

Blackink is architected not as a loose collection of disconnected chatbots, but as a unified, deterministic Agent Operating System (AOS). The human operator interacts with the platform through a centralized Slack cockpit, while the underlying events stream and outcomes ledger serve as the shared single source of truth. Deterministic code strictly governs money, legal obligations, compliance rules, and hard safety gates. The nine specialist agents operate within bounded domains—managing language generation, intent prioritization, market intelligence analysis, and recommendation drafting. Autonomous execution is only unlocked after an agent earns authority through verified accuracy.

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

**The Shared Agent Core (Chassis)**

The Shared Agent Core provides the underlying scaffolding upon which all nine specialist agents execute. Agents are implemented as declarative policies running on top of this shared chassis rather than standalone codebases.

| Subsystem                  | Technical Implementation & Invariant Rule                                                                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Tenant Isolation           | Every database row, Redis key, cache entry, vector embedding, and audit record is strictly partitioned by client_id. CI/CD executes adversarial cross-tenant leakage tests nightly.                                                                          |
| Payload-Bound Approvals    | Every Slack approval button (Approve, Revise, Reject) is cryptographically bound to a SHA-256 hash of the exact message body, recipient ID, and configuration state. An action cannot execute under an outdated click if the underlying payload has changed. |
| Cross-Agent Object Locks   | Distributed Redis leases prevent race conditions. If the Campaign Agent holds an active lock on an opportunity, the Reactivation Agent cannot initiate contradictory outreach.                                                                               |
| Idempotency Engine         | Every outbound send, calendar booking, database update, and billing trigger requires a unique idempotency key (idempotency_key = hash(tenant_id + entity_id + action_type + timestamp_bucket)) to prevent duplicate sends or double charges.                 |
| Persistent Circuit Breaker | Emergency halt controls at the global, tenant, campaign, or channel level freeze schedulers indefinitely until an authorized human issues a resume command in Slack.                                                                                         |

**Autonomy Ladder & Governance Bands**

Blackink enforces a strict governance model where autonomy is earned at the individual action-class level rather than granted globally across an entire agent. Any critical failure, data discrepancy, or compliance incident immediately demotes that action class back to mandatory human review.

| Authority Band                   | Operational Capability                                                                                   | Promotion Requirement                                                        | Demotion Trigger                                                                     |
| -------------------------------- | -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Band 1: Observe & Report         | Agent reads, analyzes, drafts, and recommends. Zero autonomous external actions permitted.               | Default baseline for all new action classes.                                 | Immediate on any critical runtime fault.                                             |
| Band 2: Class Approval (One-Tap) | Concrete action card queued in Slack for one-click human authorization (Approve/Reject).                 | 50 consecutive clean human approvals (≥95% approval rate).                  | ≥1 policy violation or approval rejection spike (>5%).                              |
| Band 3: Bounded Auto-Execution   | Reversible, low-risk actions execute autonomously within hard-coded rate, send, and spend caps.          | 250+ clean executions with <2% dispute/reversal rate and passing eval tests. | Any customer dispute, unhandled error, or reversal incident.                         |
| NEVER AUTONOMOUS                 | Permanent human-only gate: pricing terms, billing charges, refunds, legal replies, and contract closing. | No promotion path exists.                                                    | Hardcoded invariant in deterministic code. (N/A — Permanent Architectural Boundary) |

**Central Slack Hub Cockpit Topology**

| Slack Channel         | Primary Operational Purpose & Surface Interaction                                                                                                                    |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| #blackink-command     | Executive operating cockpit: macro quota queries, portfolio health summaries, emergency global pause/resume controls, and cross-client system alerts.                |
| #blackink-setter      | Human conversation cockpit: 1-screen context cards for inbound hot leads, suggested openers, objection battle-cards, and manual dial tasks.                          |
| #sales-replies        | Real-time inbound communication stream: ingests prospect emails and SMS replies with intent classification tags and one-tap triage action buttons.                   |
| #dial-tasks           | Prioritized daily calling queue: populates phone tasks with direct dial lines, timezone calculations, and Sendspark video engagement metrics.                        |
| #client-{name}-growth | Dedicated tenant growth workspace: daily quota pacing, active campaign stats, hot leads, test proposals, and referral desk updates.                                  |
| #client-{name}-launch | Dedicated onboarding channel: real-time 15-state checklist tracking, missing asset alerts, preflight status, and launch approvals.                                   |
| #blackink-qa          | Health and resilience channel: deliverability alerts, domain reputation drops, dead-letter queues, failed webhooks, and automated cross-tenant leakage test reports. |
| #blackink-economics   | Financial performance channel: per-tenant cost ledgers, contribution margins, paid wallet consumption, Economics Governor flags, and books-ready exports.            |

### 5.2 Comprehensive Specialist Agent Profiles

| Agent Name                | Core Engine Ancestry           | Primary Operational Mission                                 |
| ------------------------- | ------------------------------ | ----------------------------------------------------------- |
| 1. Launch Agent           | Launch OS / Golden Cloner      | Rapid onboarding, preflight validation & governed launch.   |
| 2. Prospecting Agent      | Hunter / Scout                 | Entity resolution, multi-LLC aggregation & owner scoring.   |
| 3. Campaign Agent         | Cora (Draft) + Relay (Execute) | Multi-touch sequencing, audit generation & bandit testing.  |
| 4. Lead Agent             | Reply Triage Agent             | <5min inbound triage, context assembly & weekend mode.      |
| 5. Reactivation & Nurture | Reactivation Engine            | Dead-lead revival, timing memory & churn save workflows.    |
| 6. Referral Agent         | Referral & Partner Desk        | B2B partner ecosystems, Realtor pipelines & Ryse attach.    |
| 7. Economics & Growth     | Vera                           | Financial reconciliation, cost ledger & governor bands.     |
| 8. QA / Watchdog Agent    | Watchdog Sentinel              | Tenancy isolation, deliverability health & self-healing.    |
| 9. Setter Copilot         | Closer Cockpit                 | 1-screen call context cards, opener logic & rubric scoring. |

#### 1. Launch Agent

**Mission:** Transform a newly signed property management contract into a fully configured, compliant, and active client tenant within 72 hours of receiving required data, while managing clean offboarding if a client churns.

```
LAUNCH AGENT WORKFLOW & STATE MACHINE

[Agreement Signed Webhook] ──► [Generate Tokenized Onboarding Portal Link]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 15-State Intake Monitoring & Auto-Chase Subsystem                                                                        │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ • Tracks States 1–7 (Core Profile, Payment Auth, Calendar Slots, Offer Approved)                                          │
│ • Tracks States 8–11 (Historical CRM Ingest, Document Upload, Partner Menu, Growth Elections)                             │
│ • Ingested items fire events to Ledger ──► Auto-chase engine immediately cancels matching reminder notifications          │
└──────────────────────────────────────────────┬───────────────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Automated Tenant Provisioning Pipeline                                                                                    │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Clones Golden Client master schema; isolates tenant via unique `client_id`                                             │
│ 2. Provisions 3-domain / 6-mailbox dedicated sending cluster with validated SPF/DKIM/DMARC                                │
│ 3. Ingests client active book into non-poach suppression database                                                          │
│ 4. Parses fee schedule & historical data ──► Compiles First-14-Days Operational Growth Plan                                │
└──────────────────────────────────────────────┬───────────────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Preflight Red-Team Verification (Asserts All 10 Core Gates Green) ──► Arms Campaigns                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Core Responsibilities:** Coordinates the 15-state onboarding checklist as a live state machine; clones the Golden Client database template, injecting tenant configuration parameters and isolating tenant credentials; assigns pre-warmed sending domains, Instantly sub-workspaces, and dedicated local Twilio phone numbers; evaluates preflight checks; auto-generates the customized First-14-Days growth plan; executes governed offboarding teardowns.

**Input Interfaces:** Signed contract webhooks, Stripe payment intents, uploaded CSV spreadsheets, fee schedule documents, and calendar credentials. **Output Interfaces:** Provisioned PostgreSQL schemas, tokenized portal links, First-14-Days plans, and Slack launch status cards in #client-launch. **Integrated Tools:** Database cloner scripts, DNS validation tools, Twilio sub-account APIs, and Google Calendar connection validators.

**Autonomy Bounds & Safety Stops:** Band 1 (Default) observes checklist progress and drafts First-14-Days plan; Band 2 (One-Tap) queues campaign arming in Slack once preflight is 100% green; Hard Stop — cannot launch campaigns if any preflight check evaluates red or payment authorization is unverified.

#### 2. Prospecting Agent (Hunter / Scout)

**Mission:** Maintain an unexhausted pipeline of qualified property owners, principals, and acquisition opportunities without exhausting market territory or violating compliance boundaries.

```
PROSPECTING AGENT RESOLUTION & SCORING PIPELINE

[Raw County Tax / Deed Feeds] ──► [LLC Entity Resolution Engine] ──► [Cross-County Owner Aggregation]
        ▼
[Maps Real Individuals across LLCs]
        ▼
[Ranked Top-40 Metro Artifact] ◄── [Deterministic Scoring Algorithm] ◄── [Calculates Total Portfolio Doors]
        ▼
[Pre-Send Non-Poach Screening] ──► [Verified Contacts Enriched] ──► [Dispatched to Campaign Queue]
```

**Core Responsibilities:** Ingests public county deed recordings, tax assessor rolls, and DBPR license registries across target Florida metros; resolves corporate LLC owners back to true individual managing members and beneficial owners; calculates the Owner Score:

> Owner Score = (Verified Doors × 15) + (Distress Multiplier × 20) + (In-Market Proximity × 10) − (Entity Fragmentation Penalty)

Monitors pipeline inventory against active client 4-week quotas; operates targeted acquisition sub-recipes: Portfolio Intercept (5+ units), Retiring-Broker Radar, Absentee Owners, Eviction Dockets, and Review-Mining switchers.

**Input Interfaces:** County property data feeds, DBPR license files, Google Maps sweeps, skip-trace APIs, and active client suppression lists. **Output Interfaces:** Verified companies and contacts database records, ranked top-40 metro owner artifacts, and source quality reports. **Integrated Tools:** Standalone Hunter resolution droplet, Tracerfy skip-trace APIs, county clerk scraper adapters, address standardization engines.

**Autonomy Bounds & Safety Stops:** Band 1 extracts data, scores entities, and drafts candidate queues; Band 2 enqueues top-decile verified owner batches for campaign ingestion; Hard Stop — hard-blocked from querying or targeting any entity present on active client suppression or non-poach lists.

#### 3. Campaign Agent (Cora & Relay)

**Mission:** Coordinate, execute, and dynamically optimize multi-touch, multi-channel growth campaigns across email, human phone prompts, LinkedIn, and transactional SMS without requiring human copy rewrites for every touch.

```
CAMPAIGN AGENT (CORA + RELAY) DISPATCH ENGINE

┌────────────────────────────────────────────────────────┐  ┌───────────────────────────────────────────────────────────┐
│ Cora (Language, Personalization & Optimization)           │  │ Relay (Execution, Idempotency & Rate Throttling)               │
├────────────────────────────────────────────────────────┤  ├───────────────────────────────────────────────────────────┤
│ • Ingests prospect profile + ghost-shopper audit score     │  │ • Enforces per-mailbox rate limits (30–50 sends/day)            │
│ • Drafts dynamic 5-touch sequenced payloads                │  │ • Injects payload-bound SHA-256 idempotency keys                │
│ • Injects Sendspark dynamic video parameters                │  │ • Manages cell-allocation rotation across active domains        │
│ • Formulates weekly copy/offer test proposals               │  │ • Halts dispatch immediately on circuit-breaker trip            │
└──────────────────────────┬─────────────────────────────┘  └─────────────────────────────┬─────────────────────────────┘
        └───────────────────────────────┬─────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Deterministic Send Gate: Lints DNC, Validates Opt-Outs & Enforces Cold Email-First Waterfall                            │
└──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Outbound Dispatch ──► Dispatches Email ──► Logs `touch_sent` Event to Shared Ledger                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Core Responsibilities:** Generates hyper-personalized 5-touch outbound sequences combining ghost-shopper response metrics, revenue loss models, and fee-stack proofs; embeds Sendspark dynamic video landing pages into cold email payloads; operates the public Rent Analysis Bot campaign trigger and self-serve audit funnel; coordinates Relay execution adapters to ensure daily mailbox sending limits and domain pacing rules are maintained; formulates weekly test proposals using contextual bandit algorithms.

**Input Interfaces:** Enriched contact records, ghost-shopper latency logs, Sendspark video IDs, and real-time open/click engagement webhooks. **Output Interfaces:** Outbound emails via Instantly APIs, phone tasks in #dial-tasks, and weekly optimization digests in Slack. **Integrated Tools:** Instantly API connectors, Sendspark dynamic video endpoints, Mailbrevo delivery helpers, Looker tracking models.

**Autonomy Bounds & Safety Stops:** Band 1 generates drafts for human approval; Band 2 dispatches approved sequence templates autonomously after 50 clean reviews; Band 3 executes minor copy and timing optimizations within send caps; Hard Stop — cold outbound SMS is hard-blocked; cannot modify commercial pricing terms or bypass daily mailbox limits.

#### 4. Lead Agent / Reply Triage

**Mission:** Ingest, classify, and route 100% of inbound communications within 5 minutes, ensuring no qualified intent is delayed or mishandled.

```
LEAD AGENT INTENT TAXONOMY & ROUTING STATE MACHINE

[Inbound Message Received via Webhook]
        ▼
┌───────────────────────────────────────┐
│ Multi-Class Intent Classifier          │
└───────────────────┬───────────────────┘
┌──────────────────┬───────────────────────────┼───────────────────────────┬──────────────────┐
▼                    ▼                            ▼                            ▼                    ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ `HOT_LEAD` /       │ │ `QUESTION` /       │ │ `LATER`            │ │ `UNSUBSCRIBE`      │ │ `LEGAL_GRIEF`      │
│ `WHALE_OWNER`      │ │ `OBJECTION`        │ │ (Timing Signal)    │ │ (Opt-Out Signal)   │ │ (Risk Signal)      │
├──────────────────┤ ├──────────────────┤ ├──────────────────┤ ├──────────────────┤ ├──────────────────┤
│ Halts cold seq;     │ │ Evaluates KB;       │ │ Ingests date to     │ │ Deterministic       │ │ Tripping circuit    │
│ builds 1-screen     │ │ drafts auto-reply   │ │ Reactivation        │ │ opt-out write to    │ │ breaker; halts      │
│ context card;       │ │ if conf ≥90%, │ │ memory; pauses      │ │ DB suppression      │ │ sequence; alerts    │
│ sets SLA timers.    │ │ else queues.        │ │ active campaign.    │ │ (Zero human).       │ │ P0 to executive.    │
└────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘
▼                    ▼                            ▼                            ▼                    ▼
#blackink-setter    #sales-replies             Reactivation Queue         Compliance Gate         #blackink-command
```

**Core Responsibilities:** Parses inbound messages across 10 operational intent classes; executes deterministic opt-out writes immediately upon detecting unsubscribe intent; generates 1-screen context cards for setter/closer queues; enforces tiered response SLAs (15/60/240 minutes); operates 24/7 after-hours and weekend mode; ingests client-side service questions and complaints with the same SLA timing.

**Input Interfaces:** Inbound email webhooks, Twilio SMS webhooks, website concierge form submissions. **Output Interfaces:** Slack context cards, automated knowledge-base replies, calendar booking links, compliance suppression writes. **Integrated Tools:** LLM intent classification pipelines, knowledge-base vector stores, Twilio SMS APIs, Calendly webhook listeners.

**Autonomy Bounds & Safety Stops:** Band 1 classifies intent and routes drafts to Slack queues; Band 2 auto-dispatches approved KB answers upon human confirmation; Band 3 sends high-confidence knowledge base responses (≥90% confidence) for routine queries; Hard Stop — all legal threats, opt-outs, and negative complaints bypass automated generation and halt outbound actions immediately.

#### 5. Reactivation & Nurture Agent

**Mission:** Maximize the lifetime value of existing data assets by systematically converting old inquiries, unclosed proposals, no-shows, and churn-risk properties into signed revenue.

```
REACTIVATION & NURTURE TIMING MEMORY ENGINE

[Inbound Timing Signal: "Call back in January"] ──► [Structured Date Extraction] ──► [Timing Memory Store]
        ▼
[Monitors Calendar Dates]
        ▼
[Outbound Re-Engagement Dispatched] ◄── [Context Re-Assembled] ◄── [Target Date Arrives (e.g., Jan 15)]
        ├── Recalls Previous Thread History & Stated Objections
        ├── References Property Address & Initial Audit Score
        └── Applies Fresh Value Hook (New Market Comps / Ryse Rent Advance)
```

**Core Responsibilities:** Maintains an active Timing Memory Store capturing future re-engagement dates; executes the Win-Back recipe against client historical dead leads; coordinates no-show recovery workflows; operates the Churn Tripwire save workflow; manages client win-back workflows for churned Blackink clients themselves.

**Input Interfaces:** CRM dead-lead exports, historical meeting logs, timing metadata from Lead Agent triage, county deed/listing alerts. **Output Interfaces:** Win-Back outbound campaigns, no-show recovery emails, retention save cards in Slack. **Integrated Tools:** PostgreSQL temporal scheduler, Instantly campaign connectors, Churn Tripwire public record monitors.

**Autonomy Bounds & Safety Stops:** Band 1 flags upcoming dates and drafts re-engagement messages; Band 2 fires win-back batches upon one-click approval; Band 3 automatically schedules future-dated re-engagement touches within established cadences; Hard Stop — cannot contact any property owner currently listed in an active client's active management database.

#### 6. Referral Agent

**Mission:** Establish a compounding local referral network by identifying, nurturing, and monetizing relationships with real estate agents, vendors, lenders, and industry partners.

```
REFERRAL & PARTNER ECOSYSTEM ENGINE

[Partner Discovery: Realtors / Lenders / Vendors] ──► [Specialized B2B Partner Outreach] ──► [Books Partner Meeting]
        ▼
[Human Manages Relationship]
        ▼
[Referral Credit Ledger Updated] ◄── [Signed Agreement Detected] ◄── [Realtor Refers Investment Owner]
        ├── Tracks $250 Client-to-Client Referral Credits
        ├── Tracks $500 New-County Referral Credits
        └── Ingests Ryse & Partner Menu Monetization Events ($50/door in 4 tranches)
```

**Core Responsibilities:** Identifies and prioritizes local professional partners; executes specialized B2B partner sequences establishing referral relationships; manages Partner Menu enrollment workflows coordinating onboarding integrations with vendors like Ryse; operates the Referral Credit Ledger; captures client wins as marketing proof (testimonial requests, case study drafts, badge nominations).

**Input Interfaces:** Local Realtor MLS rosters, vendor lists, client growth elections, verified `signed=TRUE` outcome events. **Output Interfaces:** Partner outbound sequences, referral attribution ledger entries, automated case study drafts. **Integrated Tools:** Partner CRM schemas, review aggregation APIs, Looker referral attribution models.

**Autonomy Bounds & Safety Stops:** Band 1 drafts partner outreach and logs referral sources; Band 2 enqueues testimonial and referral requests upon verified agreement milestones; Hard Stop — all vendor agreements, legal fee-sharing contracts, and financial disbursements require human execution.

#### 7. Economics & Growth Agent (Vera)

**Mission:** Continuously track unit economics, manage client quotas, enforce deterministic spending governors, calculate per-tenant contribution margins, and ensure accurate financial reporting.

```
ECONOMICS GOVERNOR & PAID WALLET CONTROLLER

[Events & Attribution Ledger Ingestion]
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Per-Client Variable Cost Ledger (`COST-LEDGER`)                                                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Calculates Net Margin: Gross Revenue - (Enrichment + Twilio + Instantly Mailboxes + LLM Tokens + Paid Media Spend)       │
└───────────────────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Deterministic Economics Governor Bands                                                                                   │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ • Green Band (<$200 / Signed Deal) ──► Fully funded; auto-allocates expansion budget                                     │
│ • Yellow Band ($200–$300 / Deal) ──► Monitored; maintains current volume                                                  │
│ • Orange Band ($300–$400 / Deal) ──► Trims high-cost enrichment and dials back ad spend                                   │
│ • Red Band (>$400 / Deal) ──► Hard pause on paid channels; flags alert to Slack #blackink-economics                       │
└───────────────────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Hard-Capped Paid Wallet Engine: Enforces strict spend limits ($150–$300 initial risk) in database                        │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Core Responsibilities:** Operates the Opportunity Quota Engine, tracking target goals, booked meetings, held appointments, signed doors, and revenue forecasts per tenant; enforces the Deterministic Economics Governor; manages the Paid Wallet Engine; calculates the Client Health Score; monitors collections and billing health; compiles monthly books-ready financial export packs.

**Input Interfaces:** Stripe transaction webhooks, Instantly mailbox costs, Twilio usage metrics, LLM token logs, appointment outcome rows. **Output Interfaces:** Daily Growth Reviews in Slack, Looker financial dashboards, books-ready CSV exports, Stripe billing commands. **Integrated Tools:** Stripe Billing API, Looker SQL modeling engine, PostgreSQL financial transaction ledger.

**Autonomy Bounds & Safety Stops:** Band 1 calculates economics, monitors margins, and drafts financial reports; Band 2 triggers dunning recovery emails and queues budget reallocations; Hard Stop — cannot modify commercial pricing tiers, issue cash refunds, or alter wallet spend ceilings without human authorization.

#### 8. QA / Watchdog Agent

**Mission:** Ensure system integrity by actively monitoring infrastructure health, validating tenant isolation, enforcing suppression rules, and auto-recovering from transient faults.

```
QA / WATCHDOG RESILIENCE & ADVERSARIAL TEST HARNESS

┌───────────────────────────────────────┐  ┌────────────────────────────────────────┐
│ Nightly Suppression & Isolation Tests   │  │ Infrastructure & Deliverability Health   │
├───────────────────────────────────────┤  ├────────────────────────────────────────┤
│ • Injects synthetic cross-tenant data   │  │ • Monitors API latency & webhooks         │
│ • Validates non-poach suppression       │  │ • Tracks domain reputation & bounce %     │
│ • Asserts row-level database security   │  │ • Detects stale data & queue delays       │
└──────────────────┬────────────────────┘  └───────────────────┬────────────────────┘
        └──────────────────────────┬───────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Automated Self-Healing & Incident Escalation Subsystem                                          │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 1. Transient API Failure ──► Retries with exponential backoff (Max 3 attempts)                    │
│ 2. Domain Reputation Degradation ──► Quarantines domain & rotates to warmed backup pool            │
│ 3. Critical Failure / Tenancy Leak ──► Trips Emergency Circuit Breaker; alerts #blackink-qa        │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Core Responsibilities:** Executes nightly automated regression suites running adversarial cross-tenant data leakage tests; audits suppression integrity; monitors deliverability infrastructure across all 20 active sending domains; evaluates system heartbeats; auto-adjudicates appointment disputes; implements self-healing routines.

**Input Interfaces:** UptimeRobot webhooks, database error logs, mail deliverability telemetry, appointment dispute tickets. **Output Interfaces:** Incident alerts in #blackink-qa, automated domain quarantine triggers, dispute verdict logs. **Integrated Tools:** Pytest adversarial test suites, Sentry error monitoring, UptimeRobot, PostgreSQL audit loggers.

**Autonomy Bounds & Safety Stops:** Band 1 monitors health metrics and posts incident alerts; Band 2 recommends mailbox domain rotations and dead-letter queue re-runs; Band 3 automatically restarts safe worker containers and quarantines degraded domains; Hard Stop — cannot override compliance gate errors or dismiss security assertion failures.

#### 9. Setter Copilot

**Mission:** Maximize the conversation-to-booking conversion rate of human setters and closers by generating real-time context briefs, recommended talk-tracks, and post-call analysis.

```
SETTER COPILOT CALL CONTEXT & TRANSCRIPT ANALYSIS

[Hot Lead / Booked Call Scheduled] ──► [Assembles 1-Screen Context Card] ──► [Posts to Slack #blackink-setter]
        ▼
[Human Conducts Phone Call]
        ▼
[Learned Conversion Patterns Stored] ◄── [Rubric Scoring & Objections] ◄── [Call Transcript Ingested]
```

**Core Responsibilities:** Assembles unified 1-Screen Context Cards for every scheduled discovery call and prospect dial; suggests tailored opening hooks, targeted discovery questions, and objection battle-cards; manages the daily calling and follow-up queue inside #blackink-setter; ingests call recordings and transcripts, scoring conversations against sales rubrics; integrates with the Referral Agent upon detecting positive sales closes.

**Input Interfaces:** Calendly booking payloads, call recording webhooks, prospect engagement telemetry, CRM thread histories. **Output Interfaces:** Context cards in #blackink-setter, drafted post-call follow-ups, transcript analysis logs. **Integrated Tools:** Transcription APIs, Block Kit interactive interfaces, calendar sync connectors.

**Autonomy Bounds & Safety Stops:** Band 1 generates call briefs, suggests talk-tracks, and drafts follow-up notes; Band 2 enqueues drafted post-call summaries and reminders for one-click dispatch; Hard Stop — all verbal discovery conversations, qualification decisions, and contract negotiations remain strictly human.

### 5.3 Cross-Agent Orchestration, Work-Orders & Locking Protocols

Every operational action across the nine agents is represented as a formal, typed Work-Order in the shared database:

```sql
-- Schema: src/db/migrations/008_work_orders.sql
CREATE TABLE agent_work_orders (
action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
entity_id UUID NOT NULL,
opportunity_id UUID,
agent_id VARCHAR(50) NOT NULL, -- e.g. 'PROSPECTING_AGENT', 'CAMPAIGN_AGENT', 'LEAD_AGENT'
action_class VARCHAR(100) NOT NULL, -- e.g. 'DISPATCH_EMAIL_TOUCH', 'ENQUEUE_DIAL_TASK'
autonomy_band VARCHAR(20) NOT NULL, -- 'BAND_1_OBSERVE', 'BAND_2_ONE_TAP', 'BAND_3_AUTO'
risk_class VARCHAR(20) NOT NULL, -- 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
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

To prevent multiple agents from executing contradictory actions on the same entity (e.g., the Campaign Agent sending a cold outreach email while the Reactivation Agent initiates a win-back sequence), the Shared Agent Core enforces distributed leases via Redis:

```python
# Location: src/core/orchestration/lease_manager.py
import redis
import hashlib
import json

class EntityLeaseManager:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def acquire_entity_lease(self, client_id: str, entity_id: str, agent_id: str, ttl_seconds: int = 300) -> bool:
        """
        Acquires an exclusive operational lease on an entity.
        Prevents competing agents from acting on the same target concurrently.
        """
        lease_key = f"lease:{client_id}:{entity_id}"
        lease_payload = json.dumps({"agent_id": agent_id, "acquired_at": str(datetime.utcnow())})
        # Atomically set key if not exists (NX) with Time-To-Live expiration (EX)
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

**End-to-End Inter-Agent Execution Trace**

| Step | Agent Acting           | Action Executed & System State Change                                                                                                                                                     |
| ---- | ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1.   | Prospecting Agent      | Ingests county records; resolves multi-LLC owners; calculates Owner Score; writes prospect record to companies and contacts.                                                              |
| 2.   | QA / Watchdog Agent    | Runs compliance lint: verifies DNC status, confirms zero cross-client non-poach conflicts, and tags record as EMAIL_COLD_ELIGIBLE.                                                        |
| 3.   | Campaign Agent         | Deploys Ghost-Shopper bot to target website; records response latency; compiles dynamic Loss PDF; injects Sendspark video ID; queues Email 1.                                             |
| 4.   | Lead Agent             | Prospect replies: "Interested, how does this work?"; classifies intent as HOT_LEAD (96% confidence); halts outbound sequence; alerts#blackink-setter.                                     |
| 5.   | Setter Copilot         | Compiles 1-screen context card (portfolio size, response audit, video watch data); closer runs discovery call; prospect books onboarding demonstration.                                   |
| 6.   | Launch Agent           | Agreement e-signs; fires tokenized Onboarding Portal link; tracks 15 states; provisions isolated database schema; arms client campaigns.                                                  |
| 7.   | Economics Agent (Vera) | Nightly PMS sync confirms newly signed property agreement (door_signed); triggers 50% initial Stripe ACH charge; compiles dynamic Evidence Packet PDF; schedules Day 60 clawback monitor. |

### 5.4 Unified Memory Spines & Continuous Learning Loops

Blackink captures operational evidence from the very first send, establishing an empirical learning foundation that compounds over time.

| Learning Loop Name           | Operational Data Stored & Continuous System Utility                                                                                                             |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Operational Memory        | Active tasks, deadlines, lease states, and dependencies. Ensures seamless state recovery after container restarts without duplicate work.                       |
| 2. Conversation Memory       | Complete communication history, objections, commitments, and sentiment tags across all channels. Eliminates repetitive or uncontextualized follow-ups.          |
| 3. Standing Client Rules     | Tenant-specific brand guidelines, forbidden claims, geographic boundaries, target fee structures, and pricing models. Enforced consistently across all agents.  |
| 4. Counterfactual Memory     | Logs human revisions, overrides, and rejected agent recommendations alongside stated reasons. Teaches agents when not to act and calibrates confidence scoring. |
| 5. Win/Loss Autopsy          | Structured teardowns of won vs. lost opportunities (lead source, door count, messaging angle, speed to lead, stated objections).                                |
| 6. Experiment Registry       | Logs every copy, subject line, and timing variant tested with sample sizes, statistical significance, and results to prevent re-testing dead ideas.             |
| 7. Source Quality Loop       | Measures match rate, cost per usable record, and signed doors per source dollar across data vendors, automatically downgrading underperforming feeds.           |
| 8. Closing / Transcript Loop | Ingests call recordings, rubric scores, and objection handling data to train Setter Copilot on conversation patterns that successfully convert.                 |

**Cross-Tenant Privacy Protection:** All learned playbooks and contextual optimizations are sanitized of personally identifiable information (PII) and tenant-specific business data. Optimization weights transfer across clients as generalized statistical heuristics, ensuring zero cross-client information leakage.

### 5.5 System Reliability, Circuit Breakers & Incident Handling

| Failure Scenario                                    | Automated Detection Trigger                                              | Deterministic Recovery Protocol                                                                                           |
| --------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Third-Party API Outage (RentCast / CoreLogic / TCR) | Provider returns 5xx status or times out for 3 consecutive requests.     | Fails gracefully to cached fallback data; labels outputs as DEGRADED; queues background retries with exponential backoff. |
| Stale Data or Missing Key                           | Data older than 30-day refresh window or required entity key is null.    | Returns explicit UNKNOWN or ABSTAIN state; prevents zero-filling; alerts#blackink-qa.                                     |
| Persistent Task Failure                             | Job fails execution across 3 retry attempts.                             | Moves payload to Dead-Letter Queue (DLQ); creates priority incident ticket in Slack.                                      |
| Domain Reputation Drop                              | Bounce rate >3% or spam complaints >0.08% within rolling 48-hour window. | Instantly quarantines degraded domain; routes traffic to warmed backup domain pool.                                       |
| Cross-Tenant Isolation Breach                       | Adversarial test or query detects record with conflicting client_id.     | Trips Emergency Circuit Breaker; freezes outbound dispatch; alerts P0 to#blackink-command.                                |
| Emergency Operator Halt                             | Human operator triggers /halt command in Slack workspace.                | Persistently freezes all schedulers and message queues in Redis/DB until explicit resume.                                 |

### 5.6 Full-System Acceptance Standard (Definition of Done)

The Blackink Governed Nine-Agent Workforce is officially accepted and certified production-ready upon passing the complete End-to-End Multi-Agent Acceptance Contract:

| Verification Scenario       | Demonstrated Operational Behavior Required for Acceptance                                                                                                                                                                                                                             |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1. Cold Growth Lifecycle    | Prospecting Agent resolves owner → Compliance Gate verifies non-poach → Campaign Agent runs ghost-shopper audit → Lead Agent triages reply → Setter Copilot prepares call card → Launch Agent onboards tenant → Economics Agent verifies PMS agreement and executes settlement. |
| 2. Churn Save Workflow      | Synthetic deed filing injected → Reactivation Agent detects listing → Alert posted to Slack → Save opportunity logged as door_saved in ledger.                                                                                                                                     |
| 3. Inbound Weekend Mode     | Concierge inquiry submitted 9:00 PM Saturday → Lead Agent classifies intent <60s → Books discovery call directly to calendar with zero human intervention.                                                                                                                          |
| 4. Auto-Dispute Resolution  | Synthetic dispute submitted → QA Watchdog Agent evaluates duration log and transcript keywords → Programmatically renders verdict in <30 seconds.                                                                                                                                   |
| 5. Adversarial Exclusion    | Synthetic cross-tenant lead injected → Non-poach compliance gate hard-blocks work creation and execution → Incident logged to#blackink-qa.                                                                                                                                          |
| 6. Persistent Halt Hold     | Emergency /halt triggered in Slack → Background workers persistently stop → System state verified unchanged across container reboot.                                                                                                                                                |
| 7. Two-Tenant Repeatability | Full lifecycle executed on Golden Client, followed by immediate clean execution on a second test tenant via written runbook with ZERO code modifications.                                                                                                                             |
