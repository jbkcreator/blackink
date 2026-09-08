# Project Blackink — Week 1 Sprint Tasks (v2 — Active Scope)

**Sprint Duration:** September 1 – September 11, 2026
**Milestone:** Marketing Live — Live Outbound Campaigns, Owner Visibility Score Reports, Automated Booking, Demo Kit Sandbox
**Team Size:** 4 Developers
**Document Version:** v2 (Source of Truth applied Sep 3 2026). See `Week1_Tasks_Dev_Split.md` for archived original scope.

---

> **Active Scope Document.** This is v2 — the working sprint document with only active tasks after Source of Truth corrections (Sep 3 2026).
> Cancelled/deferred items (Ghost Shopper, Sendspark video, Rent Analysis Bot Q1 deferral) are archived in the original `Week1_Tasks_Dev_Split.md`.

---

## Developer 1 — Data Foundation & Compliance Engine

### Task 1.1 — Core Database Schema & Ingestion Pipeline

---

#### Subtask 1.1.1 — Database Schema Design & Migration

**Description**
Author and deploy the foundational PostgreSQL migration (`001_core_spine.sql`) that creates all four primary tables — `events`, `companies`, `contacts`, and `pm_profiles` — with their full column definitions, constraints, foreign keys, and performance indices. This migration is the bedrock every other developer depends on; it must be deployed and verified before any ingestion or compliance work begins.

**Business Requirements**

- Create `events` table with: `event_id` (UUID PK), `client_id`, `owner_id`, `property_id`, `campaign_id`, `event_type`, `source`, `value_cents`, `payload` (JSONB), `occurred_at`
- Create `companies` table with: `company_id` (UUID PK), `domain` (UNIQUE NOT NULL), `company_name`, `website`, `county` (NOT NULL — replaces `market_metro`; the county is the unit of data, ranking, contract, and seat), `door_count_est`, `current_pm_software`, `status`, timestamps
- Note: the upstream `raw_prospect_pipeline` staging table preserves the legacy field name `market_metro`; the ingestion pipeline maps `market_metro` → `county` on insert into `companies`
- Create `contacts` table with: `contact_id` (UUID PK), `company_id` (FK → companies), `contact_role_type`, `first_name`, `last_name`, `title`, `email`, `email_status`, `phone`, `phone_type`, `linkedin_url`, `is_opted_out`, `dnc_clean`, `suppression_state`, `compliance_eligibility`, `last_outbound_touch_at`, timestamps
- Create `pm_profiles` table with: `profile_id` (UUID PK), `company_id` (UNIQUE FK → companies), `specialty_tags`, `languages_supported`, `asset_class_strengths`, `geographic_coverage_polygon` (JSONB), `historical_close_rate`, `average_speed_to_lead_seconds`, `show_rate_percentage`, timestamps
- Deploy indices: `idx_events_client_type` on `(client_id, event_type, occurred_at)`; `idx_contacts_lookup` on `(email, company_id, compliance_eligibility)`; `idx_companies_domain` on `(domain)`
- Migration must be idempotent (safe to re-run with `CREATE TABLE IF NOT EXISTS`)

**Definition of Done**

- [ ] Migration runs cleanly on a fresh database with zero errors or warnings
- [ ] All four tables exist with correct column types, NOT NULL constraints, and default values confirmed via `\d+ <table>`
- [ ] `companies` table has `county` column (not `market_metro`) — confirmed via `\d+ companies`
- [ ] All three indices created and confirmed via `\di`
- [ ] Foreign key cascade (`ON DELETE CASCADE`) on `contacts.company_id` and `pm_profiles.company_id` verified with a test delete
- [ ] `EXPLAIN ANALYZE` on a sample contacts lookup query shows index scan (not seq scan)
- [ ] Migration committed to version control under `src/db/migrations/001_core_spine.sql`

---

#### Subtask 1.1.2 — Staging Ingestion Pipeline & Domain Deduplication

**Description**
Build the ingestion pipeline that reads records from the `raw_prospect_pipeline` PostgreSQL staging table and normalises them into the `companies` table. The pipeline enforces apex domain uniqueness, rejecting or updating duplicate entries, and maps the legacy `market_metro` staging field to the `county` column. Target counties for launch are Hillsborough and Pinellas first.

**Business Requirements**

- Pipeline reads from `raw_prospect_pipeline` staging table; processes records in configurable batch sizes (default 100)
- Normalises apex domain before insert (strips `www.`, lowercases, strips trailing slashes) — e.g., `www.SuncoastPM.com/` → `suncoastpm.com`
- Rejects insert and logs a `DUPLICATE_DOMAIN_SKIPPED` event if `domain` already exists in `companies`; does not raise a fatal error — pipeline continues with remaining records
- Maps staging fields to `companies` columns: `company_name`, `website`, `domain`, `market_metro` → `county`, `door_count_est`, `current_pm_software`
- New company records default to `status = 'PROSPECTING'`
- Pipeline execution logged: total records read, inserted, skipped (duplicate), and failed — written to a structured log output
- Launch priority counties: **Hillsborough** and **Pinellas** (reusing existing Forced Action records). Next wave: Orange, Duval, Polk, Pasco, Lee, Brevard, Volusia, Seminole. Miami-Dade, Broward, and Palm Beach are deliberately NOT first-launch counties.
- County is the geographic unit of data and contract — no metro-based grouping or geographic logic

**Definition of Done**

- [ ] Pipeline processes a 100-record sample batch from staging without crashing; all valid records appear in `companies` with `county` populated (not `market_metro`)
- [ ] Duplicate domain test: re-running the same batch inserts 0 new rows; `DUPLICATE_DOMAIN_SKIPPED` events logged for each duplicate
- [ ] Domain normalisation confirmed: `www.TestPM.COM/` normalised to `testpm.com` before insert
- [ ] Pipeline log output shows correct counts (read / inserted / skipped / failed) for each run
- [ ] All inserted companies have `status = 'PROSPECTING'` confirmed via SELECT
- [ ] Staging `market_metro` value correctly written into `companies.county` for each inserted record
- [ ] Pipeline runnable via a single CLI command or scheduled job entry

---

#### Subtask 1.1.3 — Two-Contact Resolution & Contact Record Mapping

**Description**
Extend the ingestion pipeline to resolve and map the two required contacts for every ingested company — Contact A (`OWNER_BROKER_MD`: Managing Broker, Owner, President, CEO) and Contact B (`OFFICE_MANAGER_OPS`: Operations Manager, Lead Property Manager, Leasing Director). Each contact is written to the `contacts` table linked to its parent company, with `compliance_eligibility` defaulting to `BLOCKED` until the compliance gate promotes it.

**Business Requirements**

- For every company inserted in Subtask 1.1.2, resolve exactly two contacts from the staging record's contact fields
- Contact A role tag: `OWNER_BROKER_MD`; Contact B role tag: `OFFICE_MANAGER_OPS`
- Required staging fields for each contact: `first_name`, `last_name`, `title`, `email`, `phone`; pipeline rejects company if either contact is missing a required field and logs `CONTACT_INCOMPLETE_SKIPPED`
- All new contacts default to `compliance_eligibility = 'BLOCKED'` and `email_status = 'UNVERIFIED'`
- `dnc_clean`, `is_opted_out`, `suppression_state` all default to `FALSE`
- Contacts are linked to their parent `company_id` via FK; if the parent company insert failed, the contact is not inserted
- Pipeline does not insert duplicate contacts — uniqueness enforced on `(email, company_id)`

**Definition of Done**

- [ ] After processing a 50-company batch, each company has exactly 2 contact rows in `contacts` confirmed via GROUP BY query
- [ ] Contact roles correctly assigned: `OWNER_BROKER_MD` and `OFFICE_MANAGER_OPS` present per company
- [ ] A staging record missing Contact B's email logs `CONTACT_INCOMPLETE_SKIPPED` and skips both contacts for that company
- [ ] All contacts default to `compliance_eligibility = 'BLOCKED'` confirmed via SELECT
- [ ] Duplicate contact insert (same email + company_id) silently skipped and logged — no constraint error crashes the pipeline
- [ ] Company with failed parent insert has zero contact rows in `contacts` — orphaned contacts do not exist

---

### Task 1.2 — Deterministic Compliance Gate & Non-Poach Architecture

---

#### Subtask 1.2.1 — Global Opt-Out & Cross-Client Non-Poach Gate

**Description**
Implement the first two sequential checks of the compliance execution gate. Check 1 evaluates whether a contact has an explicit global or channel-level opt-out recorded anywhere in the system; a match results in `PERMANENTLY_BLOCKED`. Check 2 evaluates cross-client non-poach by matching the contact's domain and email against all active clients' property management books; a match results in `SUPPRESSED` and is logged to `events`.

The `evaluate_campaign_readiness()` function does NOT require `audit_speed_score_sec` or `personalized_video_id` to be populated — those ghost-shopper prerequisites have been removed. The gate evaluates opt-out, non-poach, DNC, and cooling period only.

**Business Requirements**

- Check 1 — Global/Explicit Opt-Out: returns `PERMANENTLY_BLOCKED` if `is_opted_out = TRUE` OR `suppression_state = TRUE` on the contact record
- Check 2 — Cross-Client Non-Poach: queries `client_pm_books` table; returns `SUPPRESSED` if `owner_domain = contact.domain` OR `owner_email = contact.email` matches any row
- Non-poach suppression event logged to `events` with `event_type = 'non_poach_suppressed'`, including the matching `client_id` that owns the book entry
- Both checks run as part of the `evaluate_campaign_readiness(contact_id UUID)` PostgreSQL function
- Gate checks are strictly sequential — Check 2 is only evaluated if Check 1 passes
- No LLM or AI logic involved at any point in gate evaluation
- No ghost-shopper score or video ID prerequisite — those conditions are removed from the readiness function

**Definition of Done**

- [ ] Contact with `is_opted_out = TRUE` returns `FALSE` from `evaluate_campaign_readiness()` at Check 1 — confirmed via direct function call in psql
- [ ] Contact with `suppression_state = TRUE` also returns `FALSE` at Check 1
- [ ] Contact whose domain appears in `client_pm_books` returns `FALSE` at Check 2 and logs a `non_poach_suppressed` event to `events`
- [ ] Contact who passes both checks proceeds to Check 3 (function does not short-circuit prematurely)
- [ ] Unit tests cover: opted-out contact, suppressed contact, non-poach domain match, non-poach email match, clean contact
- [ ] Confirmed via code review that `audit_speed_score_sec IS NOT NULL` and `personalized_video_id IS NOT NULL` conditions are absent from the function

---

#### Subtask 1.2.2 — DNC Registry Scrub, Quiet Hours & Warm-Channel Waterfall

**Description**
Implement the third and fourth checks of the compliance gate. Check 3 queries National and Florida state DNC registries and enforces quiet hours (no contact between 9 PM and 8 AM recipient local time). Check 4 applies the warm-channel waterfall routing — contacts who pass all checks are assigned the correct `compliance_eligibility` state (`EMAIL_COLD_ELIGIBLE` for cold prospects; `TRANSACTIONAL_SMS_ONLY` is stored in schema for future use but no outbound SMS is dispatched this sprint).

**Business Requirements**

- Check 3 — DNC & Quiet Hours: strips dial task eligibility for any contact listed on National or Florida DNC registries; quiet hours enforced by calculating recipient local time from their phone area code or stated timezone
- DNC check runs against a locally cached registry snapshot updated nightly; cache miss falls back to a live API query
- Warm-Channel Waterfall (Check 4): cold contacts (no prior inbound or booking) receive `EMAIL_COLD_ELIGIBLE` — email and human phone task only; engaged/booked contacts (inbound event or `booked_appointment_id` present) receive `TRANSACTIONAL_SMS_ONLY` stored in schema, but no outbound SMS is dispatched this sprint
- Final `compliance_eligibility` written back to `contacts` record after gate evaluation completes
- Gate result (PASS/FAIL + reason code) logged to `events` with `event_type = 'compliance_gate_evaluated'`
- `evaluate_campaign_readiness()` function returns `TRUE` only when all four checks pass

**Definition of Done**

- [ ] Contact on the Florida DNC registry returns `FALSE` from gate; `compliance_eligibility` remains `BLOCKED`
- [ ] Contact not on DNC, with no prior engagement, gets `EMAIL_COLD_ELIGIBLE` after gate evaluation
- [ ] Contact with a `booked_appointment_id` gets `TRANSACTIONAL_SMS_ONLY` written to record (schema preserved; no SMS dispatched)
- [ ] Quiet hours test: a gate evaluation at 10 PM recipient local time does not alter SMS eligibility flag
- [ ] DNC cache fallback tested: with cache empty, live API is queried and result cached for subsequent calls
- [ ] `compliance_gate_evaluated` event logged with correct reason code for each test case
- [ ] `evaluate_campaign_readiness()` integration test with all four checks passing returns `TRUE` and `EMAIL_COLD_ELIGIBLE`

---

#### Subtask 1.2.3 — Cold SMS Hard Block at DB, Application & CI/CD Level

**Description**
Implement three independent enforcement layers that make it impossible to dispatch a cold outbound SMS — a database-level constraint, an application-layer runtime linter, and a CI/CD build gate. No SMS is sent this sprint, so these layers serve as a safety foundation for the future. All three layers must independently block cold SMS; passing any single layer is not sufficient for dispatch.

**Business Requirements**

- DB layer: PostgreSQL CHECK constraint or trigger on any outbound SMS insert that rejects records where the target contact has `inbound_sms_count = 0` AND `booked_appointment_id IS NULL`
- Application layer: pre-dispatch linter function in `src/compliance/gate_evaluator.py` that validates SMS dispatch eligibility before any carrier API call is made; throws a non-recoverable `ColdSMSBlockedError` if conditions are not met
- CI/CD layer: automated test in the build pipeline that attempts to dispatch a cold SMS to a synthetic unconsented contact and asserts the build fails if that dispatch is not blocked
- Block events logged to `events` with `event_type = 'cold_sms_blocked'` including which layer caught the violation
- All three layers tested independently in the test suite

**Definition of Done**

- [ ] DB constraint: direct SQL INSERT of a cold SMS record for an unconsented contact is rejected with a constraint violation error
- [ ] Application linter: calling the SMS dispatch function for an unconsented contact raises `ColdSMSBlockedError` before any carrier API call is made (confirmed via mock assertion)
- [ ] CI/CD test: a commit containing a code path that bypasses the application linter and attempts cold SMS dispatch fails the CI build
- [ ] `cold_sms_blocked` event logged with `layer` field identifying which enforcement layer triggered for each test
- [ ] A legitimately consented contact (prior inbound SMS exists) successfully passes all three layers

---

## Developer 2 — Owner Visibility Score Engine & Report Generation

### Task 2.1 — Owner Visibility Score Engine

---

#### Subtask 2.1.1 — Public Signal Scraper & Score Calculator

**Description**
Build the engine that collects public observable signals for a target property management firm and calculates its Owner Visibility Score (0–100) using the 10-category rubric below. No pretext inquiries, no form submissions, no ghost shopping of any kind. All signals are derived from publicly accessible sources: business profiles (Google, Yelp), the firm's public website, and FL DBPR state broker licence rolls.

**Scoring Rubric**

| Category | Signal | Max Points |
|---|---|---:|
| Owner conversion readiness | Separate owner-addressed page on website | 14 |
| Owner conversion readiness | Working owner contact form, phone, and email | 10 |
| Owner conversion readiness | Mobile-friendly, HTTPS, load under 3 seconds | 6 |
| Market visibility | Review count against county median | 16 |
| Market visibility | Review recency: under 60 days = full; over 12 months = zero | 12 |
| Public reputation | Review response rate | 18 |
| Public reputation | Average star rating | 8 |
| Accessibility | Published after-hours contact route | 8 |
| Accessibility | Median review response lag | 4 |
| Credibility | Active broker licence and licence tenure | 4 |
| **Total** | | **100** |

**Business Requirements**

- Score calculator reads target firm's `website`, `domain`, and `county` from the `companies` table
- For each of the 10 signal categories, the scraper returns a signal value and a `data_coverage` flag (`PRESENT` / `ABSENT`)
- Score is computed from present signals only; overall score and `data_coverage_pct` computed as `(present_signals / 10) × 100` — displayed as, e.g., "76/100, 94% data coverage"
- Missing signal → 0 points for that category, flagged as `MISSING_DATA` in the event payload, not a fatal error
- Final score and per-category breakdown written to the `events` table with `event_type = 'owner_visibility_score_calculated'`
- Score cached monthly per firm (`company_id` + month key); alert job diffs against last month's score and posts a Slack notification to `#blackink-qa` if delta exceeds 10 points
- Ghost-shopper response latency is NOT a signal — do not submit pretext inquiries or interact with PM contact forms in any way

**Definition of Done**

- [ ] Score calculated for 3 test firms across Hillsborough / Pinellas; all 10 categories evaluated; per-category scores sum correctly to the final score
- [ ] `data_coverage_pct` displays correctly when 2 of 10 signals are absent (80% coverage shown)
- [ ] `owner_visibility_score_calculated` event written to `events` with full per-category breakdown in `payload` JSONB
- [ ] Score cached; re-running the calculator for the same firm within the same calendar month returns the cached result without re-scraping (confirmed via log showing cache hit)
- [ ] Code review confirms no form submissions, headless browser interactions with PM contact/inquiry forms, or pretext inquiries in any code path

---

#### Subtask 2.1.2 — County Rank Calculator & Peer Benchmarking

**Description**
Compute each firm's county rank against all other scored firms in the same county. Top 25 per county are published; firms below the data floor receive "insufficient data" with no rank. Generate named peer comparisons — the three lowest-scoring categories for the target firm alongside a named local competitor with a higher score in each — for use in the PDF report.

**Business Requirements**

- County rank computed as ordinal rank (1 = highest) across all scored firms whose `county` matches the target firm; ties broken by `data_coverage_pct` descending
- Only publish ranks for the top 25 firms per county; never publish a bottom list or ranks below 25th
- Below data floor (fewer than 3 scored signals present): report "insufficient data" and no numerical rank
- Named peer comparisons: identify 2–3 real named competitors in the same county with higher scores on the target's three weakest scoring categories; source from the scored firms dataset
- County rank and top-3 named peer comparisons written to the `events` payload alongside the score

**Definition of Done**

- [ ] County rank computed correctly for a test firm in Hillsborough against a seeded set of 30 peers; rank 1 = highest scorer confirmed
- [ ] A firm with only 2 scored signals displays "insufficient data" with no numerical rank (not "rank 31")
- [ ] Three lowest-category observations generated with at least one named peer comparison per weakness
- [ ] Top-25 boundary enforced: firm ranked 26th in the seeded dataset has no published rank in output

---

#### Subtask 2.1.3 — Owner Visibility Score PDF Report Compiler

**Description**
Build the branded 2-page PDF report presenting the firm's Owner Visibility Score, county rank, per-category breakdown, peer comparisons, and a revenue model. This is the primary outbound proof artifact replacing the ghost-shopper-based PDF Loss Report. All revenue estimates use public observable assumptions clearly labelled as estimates.

**Business Requirements**

- PDF Page 1: Owner Visibility Score displayed prominently (e.g., large circular gauge), county rank, data coverage percentage, and the three lowest-scoring observations with named peer comparisons
- PDF Page 2: Revenue loss model using the formula:
  `Est. Annual Lost Revenue = Monthly Leads × (1 − e^(−0.0005 × county_avg_response_lag_seconds)) × (Avg Monthly Management Fee × 12) × Avg Owner Tenure Years`
  — Since ghost shopper is cancelled, `county_avg_response_lag_seconds` is derived from the public review response lag signal (median review response lag in seconds for the firm's county). Label this estimate and its inputs explicitly.
- Default model inputs (overridable per client): 8% management fee, 30-month average owner tenure, ~$100/door/month management fee base
- Report must display all model assumptions on Page 2 — never hides defaults
- PDF branded with Blackink / `getblackink.com` branding; no `blackink.io` references anywhere in the document
- Prospect's `company_name` on the cover header
- Generated PDF stored on object storage (S3 or equivalent); URL written to the contact record; `score_pdf_generated` event logged to `events` with the storage URL in the payload

**Definition of Done**

- [ ] PDF generated for a test firm in Hillsborough; all sections populated with realistic county benchmark data
- [ ] Page 1 shows score value, county rank (or "insufficient data"), data coverage %, and 3 named peer comparisons with specific firm names
- [ ] Page 2 revenue model output manually verified against the formula using the test firm's inputs; all assumptions displayed beneath the estimate
- [ ] `getblackink.com` branding present on cover; no `blackink.io` string found anywhere in the document (confirmed via PDF text search)
- [ ] PDF URL stored in object storage and returns HTTP 200
- [ ] `score_pdf_generated` event written to `events` with correct URL in `payload`

---

### Task 2.2 — Fee-Stack One-Pager Generator

---

#### Subtask 2.2.1 — Fee-Stack One-Pager Generator (Standalone)

**Description**
Build the ADD-8-Lite Fee-Stack discovery proof document — a 1-page branded PDF mapping uncollected fee lines and ancillary revenue opportunities for a target property management firm. This component was originally bundled with Sendspark; it now runs as a standalone using the shared merge-tag template pipeline. The Fee-Stack one-pager is attached to Touch 3 (Day 4 email) as a discovery proof artifact.

**Business Requirements**

- 1-page PDF highlighting a minimum of 5 fee leakage categories: lease renewal fees, maintenance markups, tenant setup fees, pet rent share, resident benefits packages
- Per-category estimated annual uplift calculated from `door_count_est` and market averages for the firm's county
- Uses the shared merge-tag / template pipeline (same renderer as the Owner Visibility Score PDF in Subtask 2.1.3) — no independent renderer or separate PDF library
- Attached to Touch 3 (Day 4 cold email) as a discovery proof artifact alongside that email's body copy
- Fee-Stack PDF URL stored on the contact record after generation
- `fee_stack_pdf_generated` event logged to `events` with the storage URL

**Definition of Done**

- [ ] Fee-Stack one-pager generated for a test company with `door_count_est = 120`; all 5 fee categories present with non-zero estimated annual uplift values
- [ ] Per-category uplift values scale correctly with `door_count_est` (confirmed by comparing output for 50-door vs 200-door test firm)
- [ ] Confirmed via code inspection that the same renderer/template pipeline used in Subtask 2.1.3 is called — no independent PDF renderer
- [ ] Fee-Stack PDF URL stored on contact record after generation
- [ ] `fee_stack_pdf_generated` event logged to `events` with correct URL in payload
- [ ] PDF attached correctly in a test Touch 3 email payload (confirmed in email received by test inbox)

---

## Developer 3 — Outbound Sequencer & Booking Engine

### Task 3.1 — Multi-Touch Outbound Sequencer & Interim Reply Bridge

---

#### Subtask 3.1.1 — Core Sequencer Engine & Email Touch Dispatch (Touches 1, 3, 5)

**Description**
Build the core outbound sequencer orchestrator that schedules and dispatches the three cold email touches (Day 0, Day 4, Day 10) for each eligible contact. Before every email dispatch, the sequencer queues a draft approval card in `#blackink-setter`; the email is NOT sent until a human rep clicks Approve. The sequencer re-checks compliance gate state before every touch and halts permanently for any contact that opts out, replies, or becomes ineligible between touches.

**Business Requirements**

- Sequencer reads `compliance_eligibility = 'EMAIL_COLD_ELIGIBLE'` from `contacts` before dispatching each touch; re-checks at every step (not just at the start of the sequence)
- **Human approval gate (early client phase):** Before dispatching any email touch, the sequencer queues a draft Slack card in `#blackink-setter` containing the full email preview (subject, body, recipient). Email is NOT dispatched until a human rep clicks the Approve button. Reject and Snooze buttons do not send. A template class earns autonomous dispatch only after 50 consecutively approved clean sends.
- Touch 1 (Day 0): draft queued in `#blackink-setter` for approval; upon Approve, dispatches Email 1 containing the Owner Visibility Score PDF attachment and a low-friction micro-ask ("Reply YES to see where you rank in [County]"). No GIF thumbnail, no video link.
- Touch 3 (Day 4): upon Approve, dispatches Email 2 (Fee-Stack One-Pager attached); threaded as a reply to Email 1's `Message-ID`; verifies no prior opt-out or positive reply before dispatch
- Touch 5 (Day 10): upon Approve, dispatches Email 3 (County Visibility Rank & Scarcity angle); final cold touch; after dispatch, sets a 30-day cooling timestamp on the contact record
- Emails dispatched from the assigned warmed mailbox; rotated across the 6-mailbox cluster per daily volume caps (30–50 emails per mailbox per day)
- Each dispatched email logged to `events` with `event_type = 'outbound_touch_dispatched'` and full payload (template version, mailbox ID, sending domain, touch step)
- All county-level references in email copy — no metro-level language

**Definition of Done**

_Status as of 2026-09-03. Verified end-to-end via `scripts/e2e_approval_gate.py`
against live Postgres + a real Slack button click in the test workspace
(app `A0C04PGKHAL`). Both senders exist behind one `EmailSender` seam:
`SmtpEmailSender` (real smtplib delivery — Message-ID, In-Reply-To, Reply-To,
BCC, **attachments**) and `StubEmailSender`. `build_email_sender()` auto-selects
the real one when `SMTP_*` is configured, else the stub._

_**Real-delivery test performed 2026-09-03.** Wired Mandrill SMTP via `.env.test`
+ `scripts/e2e_approval_gate.py --real` and delivered an actual Touch 1 to a
real inbox (`lesly.vj@heu.ai`) through the full path: real Approve click →
compliance re-check → mailbox pick → at-most-once claim → SMTP send → `SENT` +
`Message-ID` → `outbound_touch_dispatched` event. A separate demo
(`scripts/demo_touch1_real.py`) delivered a fully-composed Touch 1 with a
generated Owner Visibility Score **PDF attached**, proving the attachment path.
So delivery + attachment are now PROVEN in production, not just designed. The
remaining open items are BLOCKED only on the real content (client copy C2, the
Dev-2 OVS/Fee-Stack PDFs), not on sequencer or transport code._

- [x] Touch 1 draft card appears in `#blackink-setter` for a test contact; email NOT dispatched until Approve is clicked — **verified** (real card posted, real Approve click flipped the order to `APPROVED` by `slack:U0BN27JB8CW`). _Card preview gap CLOSED: the card is now a proper Block Kit layout (header, To/Touch fields, subject, blockquoted body preview, attachment + run context) rather than a raw payload dict — `_email_touch_content_blocks` in `listeners.py`. Subject/body/attachment auto-fill from the work-order payload once real copy is composed there._
- [x] Approve button triggers email dispatch; Reject and Snooze leave the email unsent and log the action — **Approve→dispatch verified end-to-end.** Reject/Snooze handlers are wired (`handle_terminal_action`, `handle_snooze`) and unit-tested; their leave-unsent behaviour not yet exercised in a live click test.
- [~] Touch 1 email received in test inbox with Owner Visibility Score PDF attached; no GIF thumbnail present; no video link in body — **transport + attachment PROVEN; blocked only on the real PDF/copy.** A real Touch 1 was delivered to `lesly.vj@heu.ai` via Mandrill SMTP through the full sequencer path (`--real` mode), and a composed demo (`scripts/demo_touch1_real.py`) delivered one **with a PDF attached** (`SmtpEmailSender` now supports attachments). No GIF, no video link — confirmed. Remaining: swap the mock copy for client copy (C2) and the mock PDF for the real Dev-2 OVS PDF (Task 2.1); both are content, not code.
- [~] Touch 3 correctly threaded to Touch 1 (`In-Reply-To` matches Touch 1's `Message-ID`); Fee-Stack one-pager attached — **threading WIRED; attachment blocked on Dev 2.** `sequence_touch_dispatches.message_id` is written on `mark_sent`; `get_touch_message_id()` now performs the per-run lookup and `dispatch_touch` passes it as `in_reply_to` via `_REPLY_TO_STEP` (Touch 3→1, Touch 5→3); `SmtpEmailSender.send()` sets `In-Reply-To`/`References`. Unit-tested (`test_touch3_passes_in_reply_to_from_touch1`, `test_touch5_threads_to_touch3`). Remaining: the Dev-2 Fee-Stack attachment (Task 2.2) and a live two-touch threading run.
- [x] Compliance re-check on Touch 3: opt-out between touches prevents the Touch 3 dispatch — **logic verified.** Per-touch gate (`evaluate_touch_gate`) runs on every dispatch and returns `COMPLIANCE_BLOCK` for an opted-out/suppressed/ineligible contact (unit-tested). Full Touch-1→Touch-3 timeline e2e still to run.
- [x] Touch 5 dispatched (after approval) and 30-day cooling timestamp written — **verified live.** Dispatching the final touch sets the run to `COMPLETED` and stamps `sequence_runs.cooling_until = now + 30 days` (`complete_run_with_cooling`); `may_enroll()` then blocks re-enrollment until the window elapses. Cooling lives on the run, not the contact (wayfinder ticket 07 — the contact keeps the separate 14-day fatigue field). Live proof: `cooling_until` set 30 days out, re-enroll returned `False`.
- [x] Daily volume cap enforced: no more than 50 sends from a single mailbox in a 24-hour window — **logic verified.** Enforced inside `get_active_mailbox_for_client` via a rolling-24h count against `sequence_touch_dispatches`; a capped mailbox is never handed out and the touch DEFERS (order → `SNOOZED`, `due_at` pushed) rather than dropping. Unit-tested (`AllMailboxesCapped`); batch-of-60 e2e still to run.
- [x] `outbound_touch_dispatched` event logged for each touch with all required payload fields — **verified** (event row: `entity_type='contact'`, `actor='cold_outbound_sequencer'`, payload carries `touch_step`, `dispatch_id`, `mailbox_id`, `sending_domain`, `template_version`, `occurred_at`).

---

#### Subtask 3.1.2 — Phone Task Bridge & LinkedIn Deep-Link Generator (Touches 2 & 4)

**Description**
Implement Touch 2 (Day 1–2 phone task) and Touch 4 (Day 7 LinkedIn deep-link). Touch 2 creates an enriched actionable task card in Slack `#dial-tasks` for the setter/closer to reference during the follow-up call. Touch 4 generates the target contact's LinkedIn profile URL and copies a tailored connection note to the setter's clipboard — no automated browser automation or LinkedIn scraping permitted.

**Business Requirements**

- Touch 2 — Phone Task: creates a Slack card in `#dial-tasks` containing: prospect full name, company name, door count, direct phone number, local timezone and current local time, Owner Visibility Score, county rank, and the three lowest-scoring categories; card appears within 60 seconds of Touch 1 dispatch approval
- Touch 2 Slack card displays local calling hours compliance indicator (green if currently in valid calling hours, red if outside 8 AM–9 PM recipient local time)
- Touch 4 — LinkedIn Deep-Link: constructs the prospect's LinkedIn search URL using `first_name`, `last_name`, and `company_name`; generates a tailored connection note (template-based, merge-tag populated); copies URL and note to the setter's clipboard via a Slack interactive button
- Touch 4 is logged as a manual task in `events` with `event_type = 'linkedin_task_created'` — no headless browser or automated LinkedIn interaction of any kind
- Compliance re-check runs before both Touch 2 and Touch 4; ineligible contacts are skipped and logged

**Definition of Done**

- [ ] Touch 2 Slack card appears in `#dial-tasks` within 60 seconds of Touch 1 Approve action for a test contact; all required fields present: name, company, door count, phone, local time, Owner Visibility Score, county rank, 3 lowest-scoring categories
- [ ] Calling hours indicator on Touch 2 card shows red for a test contact whose local time is 8 PM
- [ ] Touch 4 LinkedIn URL correctly constructed for a test contact (manually verified against a real LinkedIn search)
- [ ] Clipboard copy button on Touch 4 Slack card confirmed working in browser (copies URL + connection note text)
- [ ] `linkedin_task_created` event logged in `events`; confirmed via code review that no Playwright, Puppeteer, or LinkedIn API calls exist in the Touch 4 code path
- [ ] Compliance re-check on Touch 4: opted-out contact between Touch 3 and Touch 4 skips Touch 4 and logs a `touch_skipped_compliance` event

---

#### Subtask 3.1.3 — Interim Inbound Reply Bridge & Slack Routing Cards

**Description**
Deploy the interim reply handling system active September 11–16 that routes all inbound prospect email replies in real time to `#sales-replies` in Slack as interactive action cards. Inbound SMS replies via Telnyx are also captured if a prospect initiates contact. This bridge handles inbound communication until the automated Reply Triage Agent (Week 2) goes live, ensuring no prospect reply is missed during the gap.

**Business Requirements**

- Inbound Reply Webhook Receiver ingests incoming prospect email replies (via email webhook or IMAP polling) within 30 seconds of receipt
- Inbound SMS replies received via Telnyx webhook (not Twilio); no outbound SMS is sent this sprint, but if a prospect texts inbound, the message is captured and routed
- Each inbound reply creates an interactive Slack card in `#sales-replies` containing: prospect full name, company domain, door count, full message thread history (last 3 messages), and the prospect's Owner Visibility Score (if available)
- Card includes three one-tap action buttons: `Reply in Thread` (opens a Slack thread for the rep to compose a reply), `Book Meeting` (generates a one-click Google Calendar / GHL booking link for the prospect), `Mark Opt-Out` (deterministically sets `is_opted_out = TRUE` and halts the sequence)
- `Mark Opt-Out` action is processed server-side without human intervention; updates `contacts` table and logs `opt_out_recorded` event to `events`
- Reply receipt logged to `events` with `event_type = 'inbound_reply_received'` including raw message body and detected channel
- All action buttons cryptographically hash-verified (SHA-256 payload binding) — altered payloads rejected server-side

**Definition of Done**

- [ ] Test email reply sent to the designated inbound address appears in `#sales-replies` as a Slack card within 30 seconds
- [ ] Telnyx webhook configured; a test inbound SMS (prospect-initiated) appears in `#sales-replies` within 30 seconds
- [ ] Card displays correct prospect name, company, door count, and prior message thread (verified against test data); Owner Visibility Score shown if available
- [ ] Card does NOT reference Sendspark video watch % — field is absent from the card template
- [ ] `Mark Opt-Out` button correctly sets `is_opted_out = TRUE` on the contact record and logs `opt_out_recorded` event — confirmed via SELECT after button click
- [ ] Hash verification test: clicking a card after its payload is manually modified returns the backend rejection ("This action has expired or was altered" ephemeral message — no action executed)
- [ ] `inbound_reply_received` event written to `events` with raw body and channel for each test reply

---

### Task 3.2 — Inbound Booking Engine & Show-Rate Cascade

---

#### Subtask 3.2.1 — Google Calendar / Microsoft Graph Webhook Integration & Booking Event Logging

**Description**
Integrate with Google Calendar or Microsoft Graph to capture meeting bookings via webhook. GoHighLevel (GHL) is the fallback when the client has neither a Google nor Microsoft calendar. When a prospect books, extract their details from the webhook payload, create a `meeting_booked` event in PostgreSQL, and trigger an immediate post-booking confirmation email with calendar ICS attachment. No SMS is sent at any point in the booking flow.

**Business Requirements**

- Google Calendar or Microsoft Graph webhook configured at `/api/v1/webhooks/booking`; GoHighLevel webhook as fallback when client has neither connectable calendar — authenticated via webhook signature verification for each provider
- Webhook payload extraction: prospect name, work email, door count (from booking form custom field), scheduled meeting datetime, rep's calendar slot
- `meeting_booked` event written to `events` within 5 seconds of webhook receipt; payload includes all extracted booking fields
- Immediate post-booking dispatch: branded confirmation email with Blackink logo, meeting time, agenda, and calendar ICS attachment — email only, no SMS
- If prospect is not found in `contacts` table by email, a new contact record is created with `contact_role_type = 'INBOUND_SELF_SERVE'`
- Confirmation email dispatched within 60 seconds of webhook receipt

**Definition of Done**

- [ ] Test booking via Google Calendar creates a `meeting_booked` event in `events` within 5 seconds (confirmed via SELECT with timestamp)
- [ ] Confirmation email received in test inbox within 60 seconds; ICS attachment opens correctly in Google Calendar and Outlook
- [ ] GoHighLevel fallback webhook path documented and smoke-tested with a synthetic booking payload
- [ ] No Calendly dependency present in any code path (confirmed via code review)
- [ ] Webhook rejects an invalid signature (HTTP 401); valid signature accepted (HTTP 200)
- [ ] No SMS dispatch in any booking confirmation code path (confirmed via code review)
- [ ] New contact record correctly created with `INBOUND_SELF_SERVE` role for a booking where email does not match any existing contact

---

#### Subtask 3.2.2 — Show-Rate Reminder Cascade (24-Hour & Pre-Demo Lead-In)

**Description**
Build the two-step automated reminder sequence that fires after a booking is confirmed — a 24-hour-prior email and a 30-minute pre-demo lead-in email. Each step is scheduled precisely relative to the meeting datetime and accounts for the prospect's local timezone. The pre-demo lead-in auto-attaches the prospect's Owner Visibility Score PDF and provides meeting prep context. No SMS step is included.

**Business Requirements**

- 24-hour reminder email: scheduled for exactly 24 hours before meeting start; contains personalised agenda, prospect's county-specific growth benchmarks, and a one-click reschedule link
- 30-minute pre-demo lead-in email: auto-dispatched exactly 30 minutes before scheduled meeting time; attaches the prospect's Owner Visibility Score PDF; provides agenda and meeting prep context — does NOT mention the Rent Analysis Bot, any demo phone number, or any SMS instruction
- All reminders respect prospect local timezone — times calculated from meeting datetime and prospect's phone area code or stated timezone
- Reminders scheduled as jobs in PostgreSQL (or a job queue); cancellation of the meeting (via webhook) automatically cancels all pending reminder jobs for that booking
- Each reminder dispatch logged to `events` with `event_type = 'show_rate_reminder_sent'` and `reminder_step` field (`24h_email`, `30min_email`)

**Definition of Done**

- [ ] 24-hour reminder email scheduled correctly; fires within 60 seconds of the 24-hour mark for a test booking (simulated via fast-forward); county-specific content present in body
- [ ] 30-minute pre-demo email dispatched exactly 30 minutes before a test meeting; Owner Visibility Score PDF attachment present and opens correctly
- [ ] Pre-demo email body confirmed to contain NO phone number, no Rent Analysis Bot reference, and no SMS instruction (confirmed via content assertion in test)
- [ ] Meeting cancellation webhook cancels both pending reminder jobs — confirmed via job queue inspection showing jobs removed
- [ ] `show_rate_reminder_sent` event logged for each reminder with correct `reminder_step` value
- [ ] No SMS dispatch in any reminder code path (confirmed via code review)

---

#### Subtask 3.2.3 — No-Show Handler & Owner Score Self-Serve Landing Page

**Description**
Build the no-show detection and recovery flow that activates when a prospect fails to attend their scheduled meeting. Separately, deploy the public Owner Score Self-Serve Landing Page at `audit.getblackink.com` where prospects can submit their company domain to trigger an Owner Visibility Score calculation and book directly — with Meta and Google tracking pixels embedded for retargeting.

**Business Requirements**

- No-show trigger: rep clicks "Mark No-Show" in Slack within 10 minutes of meeting start time; system immediately pauses all outbound sequences for that contact
- Automated recovery flow enqueued upon no-show: multi-channel friction-free re-booking sequence dispatched via email only (no SMS); re-booking link to Google Calendar / GHL booking page embedded in first recovery message
- Self-serve Landing Page: publicly accessible at `audit.getblackink.com`; accepts corporate domain input via a form field; captures visitor's name, email, and company via a lightweight pre-capture form
- Landing page submission triggers a background **Owner Visibility Score** worker for the entered domain (not a ghost-shopper job); logs `owner_score_self_serve_triggered` event to `events`
- High-intent visitors (form submitted) are redirected to the Google Calendar / GoHighLevel booking page with pre-filled contact details
- Meta Pixel and Google Tag tracking pixels embedded; pixel fires on form submission only (not on page load)

**Definition of Done**

- [ ] "Mark No-Show" Slack button pauses the test contact's outbound sequence (confirmed by checking sequence status in DB)
- [ ] Recovery flow email dispatched within 5 minutes of no-show trigger; contains Google Calendar / GHL re-booking link; no SMS in recovery path
- [ ] Self-serve landing page loads at `audit.getblackink.com`; domain form submits and triggers an `owner_score_self_serve_triggered` event (confirmed in `events` table)
- [ ] Prospect redirected to Google Calendar / GHL booking page after form submission; email field pre-filled (confirmed in browser)
- [ ] Meta Pixel fires on form submission only — confirmed via browser network tab showing pixel request after submit, not on page load
- [ ] Landing page tested on mobile viewport (375px width); form and CTA fully usable
- [ ] No `blackink.io` references anywhere in landing page code or copy

---

## Developer 4 — Demo Sandbox & Metrics Engine

### Task 4.1 — Permanent Demo Sandbox

*(Rent Analysis Bot subtasks are deferred to Q1. No RentCast, CoreLogic, or Telnyx phone number provisioning in this sprint. Only the sandbox setup is active.)*

---

#### Subtask 4.1.1 — Demo Friday Sandbox Population & Configuration

**Description**
Provision and populate the Permanent Demo Friday Sandbox — a permanently designated, isolated client environment pre-loaded with realistic operational data that mirrors a real Florida property management client in Hillsborough and Pinellas counties. The sandbox must be ready for live demonstration: Looker dashboards active, mock campaigns running, and sample Owner Visibility Score evidence packets accessible on demand during sales calls.

**Business Requirements**

- Sandbox is an isolated tenant in the database with a dedicated `client_id`; named `DEMO_FRIDAY_SANDBOX` in the `companies` table
- Populated with: minimum 50 realistic mock contacts (Florida PM owners in Hillsborough and Pinellas), 3 active mock campaign records, 10 sample Owner Visibility Score PDFs, at least 5 mock `meeting_booked` events, and 20 mock outbound touch events across the 5-step sequence
- Looker Studio dashboard connected to a read-replica view of sandbox data; displays: pipeline summary (scores generated, opens, clicks, bookings), mock rep performance metrics, and active campaign status
- Sandbox data must look realistic — company names, addresses, and door counts should match actual Hillsborough and Pinellas PM market data (no "Test Company 1" placeholder names)
- Sandbox is permanent — not deleted or reset between sessions; safe to update but not wipe
- Pre-demo lead-in email tested against sandbox: fires 30 minutes before a scheduled test meeting, attaches the correct sandbox Owner Visibility Score PDF, and contains no Rent Analysis Bot reference or SMS instruction in the email body

**Definition of Done**

- [ ] Sandbox `client_id` present in `companies` table with `DEMO_FRIDAY_SANDBOX` name
- [ ] 50 mock contacts confirmed in `contacts` table linked to Hillsborough / Pinellas sandbox companies
- [ ] Looker dashboard loads with real sandbox data visible (not empty panels); all metric tiles populated
- [ ] 3 active campaign records and 10 Owner Visibility Score PDFs accessible in the sandbox environment
- [ ] Demo meeting scheduled in the sandbox triggers the 30-minute pre-demo lead-in email with the correct sandbox Owner Visibility Score PDF attached
- [ ] Pre-demo email body contains no Rent Analysis Bot phone number, no SMS instruction, and no `blackink.io` domain reference
- [ ] Sandbox data passes a realism check: no placeholder names; company names, counties, and door counts are plausible Hillsborough / Pinellas PM businesses

---

### Task 4.2 — Pipeline Metrics Engine & Slack Operations Hub

---

#### Subtask 4.2.1 — Structured Event Logging Layer & Payload Schema Enforcement

**Description**
Build the application-layer event logging service that every other Week 1 component calls to write structured payloads to the `events` table. Enforce the required payload schema — every event must include a validated set of fields appropriate to its `event_type`. The logging layer is the single shared write path; no component writes directly to `events` bypassing this service.

**Business Requirements**

- Centralised `EventLogger` service (e.g., `src/events/logger.py`) exposes a `log_event(client_id, event_type, source, payload, owner_id=None, campaign_id=None, value_cents=0)` interface
- Payload schema validated at write time: missing or null required fields for a given `event_type` raise a `MalformedEventError` and are rejected (not silently dropped)
- Required payload fields for `outbound_touch_dispatched` events: `touch_step`, `channel`, `recipient_email`, `template_version`, `sending_domain`, `mailbox_id`
- Required payload fields for `owner_score_generated` events: `score_total`, `county`, `data_coverage_pct`, `county_rank`
- Write path is async-safe: concurrent event writes from multiple workers do not deadlock or drop events
- Failed writes (DB unavailable) are queued in a local buffer (max 1,000 events) and retried; events older than 1 hour in the buffer are logged to `#blackink-qa` as data loss alerts

**Definition of Done**

- [ ] `EventLogger.log_event()` successfully writes a test `outbound_touch_dispatched` event with all required fields to `events` (confirmed via SELECT)
- [ ] `MalformedEventError` raised when `touch_step` is omitted from an `outbound_touch_dispatched` payload — event not written to DB
- [ ] `owner_score_generated` event with all four required fields (`score_total`, `county`, `data_coverage_pct`, `county_rank`) writes successfully; omitting any one raises `MalformedEventError`
- [ ] Concurrent write test: 100 simultaneous `log_event()` calls complete without deadlock; all 100 events present in `events` table
- [ ] DB unavailable test: 10 events written during a simulated DB outage are successfully flushed to `events` when DB comes back online
- [ ] No other Week 1 component has direct SQL INSERT calls into `events` — confirmed via grep of the codebase

---

#### Subtask 4.2.2 — 60-Second Post-Meeting Slack Modal & Owner Score Feed

**Description**
Build the post-meeting data capture Slack modal that closers complete within 60 seconds of every meeting. The modal collects structured outcome data — attendance status, PM software used, door count, stated objections, and next action — and writes a `meeting_outcome_recorded` event to `events`. The captured data feeds directly into the Owner Score ranking engine.

**Business Requirements**

- Slack modal triggered by the closer clicking a "Log Outcome" button in `#blackink-setter` after a meeting; modal must render within 2 seconds of button click
- Modal fields: Meeting Attendance Status (dropdown: Held / No-Show / Rescheduled), Target PM Software (dropdown: AppFolio / Buildium / Propertyware / Rent Manager / Other / Unknown), Estimated Door Count (number input), Stated Objections (multi-select: Pricing / Software Integration / Capacity / Existing Agency), Next Action (free text, 280 char max)
- Modal submission writes a `meeting_outcome_recorded` event to `events` with all five fields in the payload
- If attendance status is `No-Show`, modal submission also triggers the no-show handler (Subtask 3.2.3) for the linked contact
- Structured objection data written to a `prospect_objections` field on the contact record for use by the Owner Score engine
- Modal submission is idempotent — submitting twice for the same meeting does not create a duplicate event; second submission updates the existing `meeting_outcome_recorded` event

**Definition of Done**

- [ ] "Log Outcome" button in `#blackink-setter` opens the Slack modal within 2 seconds
- [ ] Modal submission writes a `meeting_outcome_recorded` event with all five fields confirmed via SELECT
- [ ] No-Show selection on modal submission triggers the no-show handler — outbound sequence paused for the linked contact (confirmed in DB)
- [ ] Idempotency test: submitting the modal twice for the same meeting results in one event row with the updated values (not two rows)
- [ ] `prospect_objections` field on the contact record updated with the multi-select values after modal submission

---

#### Subtask 4.2.3 — Daily Slack Digest, SHA-256 Hash Verification & Operational Channel Setup

**Description**
Deploy the automated daily Slack metrics digest to `#blackink-command`, implement SHA-256 payload-bound hash verification across all interactive Slack cards, and configure all five operational Slack channels with the `@Blackink` router.

**Business Requirements**

- Daily digest posted to `#blackink-command` each morning at a configurable time (default 8:00 AM EST); contains: Owner Visibility Scores generated (last 24h), county rank reports delivered, cold emails dispatched, open rate (%), click-through rate (%), reply rate (%), and appointments booked
- Digest data pulled from `events` table via read-replica query; if read-replica is unavailable, digest posts a "Data Unavailable" notice rather than failing silently
- SHA-256 hash verification: every interactive Slack card (Approve, Revise, Reject, Snooze, Skip, Mark Done, Mark Opt-Out) generates a `card_hash = SHA-256(payload + recipient_id + config_state + timestamp_window)`; backend validates hash on every button interaction; mismatch returns an ephemeral Slack message "This action has expired or was altered"
- Hash timestamp window: cards expire after 24 hours; interactions on expired cards rejected
- Five operational channels configured in the Slack workspace: `#blackink-command`, `#blackink-setter`, `#sales-replies`, `#dial-tasks`, `#blackink-qa`; `@Blackink` router app installed and permissions granted in all five
- Each channel receives correctly routed test messages from `@Blackink` for its designated event type

**Definition of Done**

- [ ] Daily digest fires at the configured time and appears in `#blackink-command` with all 7 metrics populated (tested with synthetic event data); metrics are: Scores generated, county rank reports, emails dispatched, open rate, CTR, reply rate, appointments booked — not ghost-shopper latency or video completion rate
- [ ] Digest posts "Data Unavailable" notice when the read-replica is simulated as offline
- [ ] SHA-256 hash verification: clicking an interactive card after manually altering its payload returns the "This action has expired or was altered" ephemeral message — no action is executed
- [ ] Expired card test: clicking a card with a timestamp older than 24 hours is rejected
- [ ] All five Slack channels exist in the workspace; `@Blackink` app active in each
- [ ] Test messages correctly routed: an `owner_score_generated` failure posts to `#blackink-qa`; a `HOT_LEAD` simulated event posts to `#blackink-setter`; a daily digest posts to `#blackink-command`

---

## Week 1 — Shared Definition of Done (Gate: September 11, 2026)

All four developers must collectively demonstrate the following live on September 11:

1. **Live Outbound Dispatch** — Automated multi-touch email sequence queues a draft in `#blackink-setter` for human approval; confirmed email dispatches from warmed Google Workspace / Outlook inboxes across dedicated domains with verified SPF/DKIM/DMARC after Approve is clicked. Demonstrate that Reject and Snooze prevent dispatch.

2. **Owner Visibility Score Report** — Generate a public-observable Owner Visibility Score for a real target PM firm in Hillsborough or Pinellas county using the 10-category public scoring rubric; verify the PDF report displays score, county rank, data coverage percentage, named peer comparisons, and model assumptions. Confirm via code review that no ghost-shopper or pretext inquiry exists in the code path.

3. **Interactive Booking Flow** — Complete a live meeting booking via Google Calendar integration; verify `meeting_booked` event in PostgreSQL; confirm immediate email confirmation with calendar ICS attachment. Confirm no SMS is dispatched anywhere in the booking flow.

4. **Lead-In Automation** — Trigger the 30-minute pre-demo lead-in email; verify the Owner Visibility Score PDF is attached; confirm the email body contains no rent bot phone number, no SMS instruction, and no `blackink.io` domain reference.

5. **Interactive Slack Hub** — Execute Approve, Revise, and Snooze actions in `#blackink-setter` and `#sales-replies`; verify payload-bound SHA-256 hash security blocks altered payloads; verify expired-card rejection.

6. **Zero Cold SMS Compliance** — Run CI/CD compliance suite; confirm cold outbound SMS is hard-blocked at the database, application, and CI/CD levels for all unconsented contacts.
