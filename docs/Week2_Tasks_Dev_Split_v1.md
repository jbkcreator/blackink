# Project Blackink — Week 2 Sprint Tasks (v1 — Active Scope)

**Sprint Duration:** September 14 – September 18, 2026
**Milestone:** Founding Client Pilot Ready — Appointment State Machine, Settlement Rails with Billing Rules, Reply Triage Agent, Win-Back with Assessor/FRBO Match, Speed-to-Lead with Six-Attempt Cadence & Pay-Per-Lead Routing
**Team Size:** 4 Developers
**Document Version:** v1 (Source of Truth: Blueprint v2 + Sept 04 client comments)

---

> **Active Scope Document.** All tasks derived from the updated Week 2 blueprint (Sept 04 aligned).
> Deferred items not in this sprint: referral partner system, same-owner assessor match, homestead signal on Owner Packet, whale ranking, post-sit nurture, HOA rental-cap flag, direct mail merge, dead-lead 9-touch SMS drip (open conflict with no-SMS-this-year rule — blocked pending client resolution).

---

## Developer 1 — Settlement Rails, Billing Rules & Appointment Infrastructure

### Task 1.1 — Appointment State Machine Schema

---

#### Subtask 1.1.1 — Deploy Appointment Tables & State Enum (`009_appointment_ops.sql`)

**Description**
Deploy the appointment infrastructure schema that the settlement billing gate depends on. The `appointments` table carries a computed `is_billable` column that evaluates to `TRUE` only when both `confirmed_24h_timestamp` and `confirmed_3h_timestamp` are non-null and state is `ATTENDED`. Full 4-rule qualification verification lands in Week 4; this week establishes the schema and state machine so settlement can issue invoices correctly.

**Business Requirements**

- Create `appointment_state_enum`: `BOOKED`, `CONFIRMED_24H`, `CONFIRMED_3H`, `ATTENDED`, `DISPOSITIONED`, `RESCHEDULED`, `NO_SHOW_RECOVERY`, `REBOOKED`, `LOST`
- Create `appointments` table with: `appointment_id` (UUID PK), `client_id` (FK → companies), `opportunity_id` (UUID NOT NULL — preserved across reschedules to prevent duplicate billing), `contact_id` (FK → contacts), `state` (enum, default `BOOKED`), `reschedule_count` (INT, default 0), `scheduled_for`, `attended_at`, `is_billable` (BOOLEAN GENERATED ALWAYS — `state = 'ATTENDED' AND confirmed_24h_timestamp IS NOT NULL AND confirmed_3h_timestamp IS NOT NULL`), `confirmed_24h_timestamp`, `confirmed_3h_timestamp`, `owner_brief_url` (TEXT NOT NULL), timestamps
- Create `confirmation_logs` table: `log_id`, `appointment_id` (FK), `channel` (`CHECK IN ('SMS', 'EMAIL')`), `confirmation_tier` (`CHECK IN ('24H', '3H')`), `sent_at`, `delivery_status`, `reply_received_at`, `raw_response`
- Create `appointment_dispositions` table: `disposition_id`, `appointment_id` (UNIQUE FK), `outcome` (`CHECK IN ('SIGNED', 'DECIDING', 'NO', 'NOT_A_FIT')`), `doors_signed`, `close_reason`, `brief_accurate` (`CHECK IN ('YES', 'PARTLY', 'NO')`)
- Create `appointment_disputes` table: `dispute_id`, `appointment_id` (UNIQUE FK), `flagged_at`, `reason`, `evidence_ref`, `outcome` (default `CREDITED_AUTOMATIC`), `resolved_at`
- Deploy indices: `idx_appointments_billing_gate ON appointments(state, confirmed_24h_timestamp, confirmed_3h_timestamp)` and `idx_opportunity_dedupe ON appointments(opportunity_id)`
- State machine rules enforced in application layer: reschedule capped at 2 (`reschedule_count > 2` → state forced to `LOST`); no-show recovery retains original `opportunity_id`

**Definition of Done**

- [ ] Migration runs cleanly on a fresh database with zero errors; all four tables exist with correct column types confirmed via `\d+`
- [ ] `is_billable` computed column evaluates `TRUE` only when state is `ATTENDED` AND both confirmation timestamps are non-null — verified with three test rows covering all gate combinations
- [ ] An appointment rescheduled twice retains its original `opportunity_id`; a third reschedule sets state to `LOST` (confirmed via application-layer test)
- [ ] `channel IN ('SMS', 'EMAIL')` CHECK constraint on `confirmation_logs` rejects any other value — confirmed via direct INSERT attempt
- [ ] `confirmation_tier IN ('24H', '3H')` CHECK constraint enforced — confirmed via direct INSERT attempt
- [ ] Both indices created and confirmed via `\di`; `EXPLAIN ANALYZE` on a billing gate query shows index scan
- [ ] Migration committed under `src/db/migrations/009_appointment_ops.sql`

---

### Task 1.2 — Stripe Settlement Rails & Billing Rules

---

#### Subtask 1.2.1 — Zero-Deposit Card Auth & ACH Mandate Capture

**Description**
Build the Stripe onboarding payment capture flow. Clients submit card and bank details through a Stripe Elements modal during onboarding. A temporary $1 authorization hold verifies card validity without charging. An ACH Direct Debit mandate is established as the primary billing rail; the card is retained as backup. Zero dollars are charged upfront.

**Business Requirements**

- Stripe Elements modal embedded in the onboarding portal; collects card details (primary) and bank account details (ACH mandate) in a single step
- Card: create a Stripe `SetupIntent` with `usage = 'off_session'`; capture the resulting `payment_method_id` on the client record
- ACH: create a Stripe `SetupIntent` for ACH Direct Debit; store the resulting bank mandate reference
- Temporary $1 authorization hold on the card to verify validity; hold released within 7 days without capture; not reflected as a charge on the client's statement
- Zero dollars charged at this step — confirmed by the billing gate (`ZERO DOLLARS CHARGED UPFRONT` invariant)
- Card and ACH payment method IDs stored encrypted on the `companies` row (not in plaintext); Stripe customer ID linked
- `payment_auth_completed` event written to `events` on successful capture; `payment_auth_failed` event on failure with error code

**Definition of Done**

- [ ] Test submission via Stripe Elements creates a `SetupIntent` for both card and ACH; both `payment_method_id` values stored on the client record
- [ ] Stripe dashboard confirms $1 auth hold present and not captured for the test card
- [ ] Zero actual charges appear on the test Stripe account after onboarding (confirmed via Stripe dashboard)
- [ ] `payment_auth_completed` event written to `events` with Stripe customer ID in payload
- [ ] Invalid card (4000 0000 0000 0002 decline) logs `payment_auth_failed` with Stripe error code — no crash
- [ ] Payment method IDs confirmed stored encrypted — plaintext values not present in the `companies` table via SELECT

---

#### Subtask 1.2.2 — 50/50 Settlement Split Engine & 60-Day Clawback Monitor

**Description**
Build the billing split engine that charges the client in two installments — 50% at the `door_signed` event (PMS verification), 50% at day 60. If the management agreement is terminated within 60 days, the second installment is automatically voided. Every charge auto-compiles and attaches a 4-section Evidence Packet PDF to the invoice.

**Business Requirements**

- Trigger: nightly PMS verification sync detects a `door_signed` event for a new management agreement — initiates the settlement pipeline
- Installment 1 (50%): ACH charge via Stripe (card as fallback) immediately upon `door_signed` verification; `settlement_transactions.installment_1_status` set to `CHARGED`
- Installment 2 (50%): scheduled as a PostgreSQL job for exactly 60 days after Installment 1; `installment_2_status = 'SCHEDULED'`
- Day 60 check: nightly job re-verifies agreement still active in PMS; if active → charge Installment 2; if cancelled → void scheduled charge, set `is_clawed_back = TRUE`, log `settlement_clawback_executed` event
- Evidence Packet PDF auto-compiled on every charge with four sections: Source & Outreach Lineage, Engagement & Booking Record, Meeting & Qualification Verification, PMS Contract Verification; PDF URL attached to Stripe invoice metadata
- Schema: `settlement_transactions` table with `transaction_id`, `client_id`, `owner_id`, `property_id`, `door_count`, `total_bounty_cents`, `installment_1_cents`, `installment_2_cents`, both installment statuses and timestamps, `evidence_packet_url`, `stripe_invoice_id`, `is_clawed_back`

**Definition of Done**

- [ ] Synthetic `door_signed` event triggers Installment 1 ACH charge in Stripe test mode; `installment_1_status = 'CHARGED'` confirmed in `settlement_transactions`
- [ ] Installment 2 job scheduled 60 days out; confirmed via job queue inspection showing correct fire datetime
- [ ] Day 60 active agreement: Installment 2 charges; `installment_2_status = 'CHARGED'` confirmed
- [ ] Day 60 cancelled agreement (simulated): Installment 2 voided; `is_clawed_back = TRUE` and `settlement_clawback_executed` event logged
- [ ] Evidence Packet PDF generated; contains all four sections; URL attached to Stripe invoice metadata (confirmed via Stripe dashboard)
- [ ] Zero dollars charged upfront in any code path — confirmed via code review that no charge fires before a verified `door_signed` event

---

#### Subtask 1.2.3 — Six Billing Rules Implementation

**Description**
Implement the six billing rules introduced in the Sept 04 client comments. All rules are stored as `entitlement_offers` rows and billing-job conditions — no pricing logic compiled into application branches. These rules are prerequisites for the founding client pilot.

**Business Requirements**

- **$50 miss credit:** Billing job checks `ack_latency_seconds > 60` on every `inbound_messages` event where `channel = 'EMAIL'` and `detected_intent` is not yet classified; if threshold exceeded, writes a `$50` credit line to the client's next invoice automatically. Both the miss event and the credit are visible on the proof ledger. No human approval required.
- **First sit free:** `first_sit_consumed BOOLEAN DEFAULT FALSE` added to the client entitlement row. Billing job checks this flag before charging `appt_standard`; if `FALSE`, charges $0 and flips the flag to `TRUE`. Enforced via entitlement ledger — cannot be overridden per-invoice.
- **60-day guarantee:** Scheduled job at day 60 counts qualifying `attended = TRUE` sits for Owner Growth and Full County accounts. If count < 4 → writes a $0 subscription override for the next billing cycle; `guarantee_applied BOOLEAN DEFAULT FALSE` flips to `TRUE` to prevent repeat. One-time per account.
- **Dispute credit on flagging:** `appointment_disputes.outcome` defaults to `CREDITED_AUTOMATIC`. Credit is issued at `flagged_at` timestamp, not `resolved_at`. The credit line is written to the invoice the moment the dispute row is created.
- **No monthly ceiling:** `monthly_cap = NULL` on `appt_standard` and `appt_first` entitlement rows. Billing gate confirmed to contain no `COUNT(*) >= cap` check for attended sits.
- **`founding = true` flag:** `founding BOOLEAN DEFAULT FALSE` added to `companies` table (client rows). Billing job predicate: `WHERE founding = TRUE` → skip rate migration on any pricing update. Set to `TRUE` at account creation for all September founding clients.

**Definition of Done**

- [ ] $50 miss credit: trigger an `inbound_messages` event with `ack_latency_seconds = 90`; confirm a `$50` credit line appears on the client's next invoice; confirm miss event and credit both visible on proof ledger
- [ ] First sit free: first attended appointment for a test account charges $0; `first_sit_consumed` flips to `TRUE`; second attended appointment charges $99 (confirmed in Stripe test mode)
- [ ] 60-day guarantee: simulate a day-60 check with 3 qualified sits; confirm next month subscription writes as $0; `guarantee_applied = TRUE`; simulate with 5 sits → no override applied
- [ ] Dispute credit on flagging: insert a dispute row; confirm credit line written immediately at `flagged_at`; confirm no second credit at `resolved_at`
- [ ] No ceiling: confirm `monthly_cap = NULL` on both entitlement rows via SELECT; submit 50 attended sits for a test client in one month — all 50 charge without cap block
- [ ] `founding = true` flag: create a founding client; run a pricing migration; confirm founding account price is unchanged while a non-founding account updates

---

## Developer 2 — Reply Triage Agent & Inbound Intelligence

### Task 2.1 — Reply Triage Agent

---

#### Subtask 2.1.1 — Intent Classifier & 10-Class Routing Engine

**Description**
Build the automated inbound intent classification engine that processes 100% of incoming email replies and classifies them into one of 10 intent classes. The classifier reads the cleaned message body, assigns a confidence score, and triggers the correct automated action for each class. Deterministic actions (opt-out, legal halt) bypass the classifier and execute directly on keyword detection — no LLM decision point for these paths.

**Business Requirements**

- 10 intent classes with their automated actions:

| Intent Class    | Automated Action                                                                   | Routing                                   |
| --------------- | ---------------------------------------------------------------------------------- | ----------------------------------------- |
| `HOT_LEAD`    | Halt cold sequence; generate 1-screen context card                                 | `#blackink-setter` + direct rep alert   |
| `QUESTION`    | Evaluate KB; draft auto-response if confidence ≥90%; else queue for human review  | Thread in `#sales-replies`              |
| `OBJECTION`   | Pull objection handling playbook; equip Setter Copilot card                        | Context card in `#blackink-setter`      |
| `LATER`       | Ingest re-engagement date into Reactivation memory; pause campaign until that date | Reactivation queue                        |
| `NURTURE`     | Transition to low-frequency monthly educational sequence                           | Nurture campaign stream                   |
| `UNSUBSCRIBE` | Deterministic opt-out:`is_opted_out = TRUE` across entity and domain — no LLM   | Compliance ledger                         |
| `COMPLAINT`   | Halt sequence immediately; suppress apex domain globally                           | `#blackink-qa`                          |
| `LEGAL_GRIEF` | Hard circuit-breaker trip; freeze all contacts; executive Slack alert (P0)         | `#blackink-command`                     |
| `WHALE_OWNER` | VIP routing; instant closer alert; high-priority SLA timer locked                  | Direct closer alert +`#blackink-setter` |
| `PARTNER`     | Route to Referral Agent; log partner type                                          | `#client-growth`                        |

- Inbound message schema (`002_triage_routing.sql`): `inbound_messages` table with `message_id`, `client_id`, `contact_id` (FK), `channel`, `raw_payload`, `cleaned_body`, `detected_intent`, `confidence_score`, `requires_human_review`, `sla_due_at`, `status`, `received_at`
- `UNSUBSCRIBE` and `LEGAL_GRIEF` are deterministic keyword matches — they do not pass through the LLM classifier; they execute immediately
- Classification result and confidence score written to `inbound_messages.detected_intent` and `inbound_messages.confidence_score`
- `inbound_reply_classified` event written to `events` with full classification payload

**Definition of Done**

- [ ] Process a 50-reply test batch across all 10 intent classes; ≥80% classification accuracy confirmed against ground-truth labels
- [ ] `UNSUBSCRIBE` keyword in a message body writes `is_opted_out = TRUE` on the contact record within 2 seconds — no LLM call in the code path (confirmed via code review)
- [ ] `LEGAL_GRIEF` detection trips a circuit-breaker flag on the client's sequences; P0 alert posted to `#blackink-command` (confirmed in test Slack workspace)
- [ ] `HOT_LEAD` classified reply halts the outbound sequence for that contact — `sequence_status = 'HALTED'` confirmed in DB
- [ ] `QUESTION` with classifier confidence ≥90% produces a drafted KB auto-response; confidence <90% sets `requires_human_review = TRUE`
- [ ] `inbound_reply_classified` event written to `events` for each test reply with correct `detected_intent` and `confidence_score`

---

#### Subtask 2.1.2 — 1-Screen Context Cards & SLA Escalation Timers

**Description**
Build the Slack context card generator that surfaces HOT_LEAD, OBJECTION, and WHALE_OWNER replies as actionable 1-screen briefings in `#blackink-setter`. Each card contains the prospect's full profile, engagement history, and owner visibility score. Implement the three-tier SLA escalation timer that escalates unclaimed hot leads automatically.

**Business Requirements**

- Context card for `HOT_LEAD` and `WHALE_OWNER` contains: prospect full name, company, door count, Owner Visibility Score, county rank, 3 lowest-scoring categories, full message thread (last 3 messages), engagement timeline (touches sent, opens, clicks), and suggested opening response
- Context card for `OBJECTION` additionally includes: objection handling playbook for the detected objection type (pricing/timing/existing agency/capacity)
- SLA timers set on `inbound_messages.sla_due_at` at classification time:
  - `HOT_LEAD` and `WHALE_OWNER`: `sla_due_at = received_at + 15 minutes`
  - All other human-review classes: `sla_due_at = received_at + 60 minutes`
- SLA escalation: 15 minutes unclaimed → second high-priority ping in `#blackink-setter`; 60 minutes unclaimed → executive mobile notification to `#blackink-command`; 240 minutes unclaimed → reallocate lead to backup closer queue
- All context cards carry payload-bound SHA-256 hash verification (same mechanism as Week 1 interactive cards); expired cards (>24h) rejected
- `context_card_generated` event written to `events`

**Definition of Done**

- [ ] `HOT_LEAD` reply generates a context card in `#blackink-setter` within 60 seconds; card contains all required fields (name, company, doors, OVS score, county rank, 3 weakest categories, thread history, opener suggestion)
- [ ] `OBJECTION` card includes an objection-type-specific playbook entry (confirmed by inspecting card content for a "Pricing" objection test case)
- [ ] SLA timer: a `HOT_LEAD` card unclaimed for 15 minutes generates a second ping in `#blackink-setter` (simulated via time fast-forward in test)
- [ ] 60-minute escalation posts to `#blackink-command` with the unclaimed lead details
- [ ] SHA-256 hash verification: altering a card payload after generation returns ephemeral rejection — no action executed
- [ ] `context_card_generated` event written to `events` for each card with `intent_class` and `contact_id` in payload

---

#### Subtask 2.1.3 — Knowledge Base Auto-Response Engine

**Description**
Build the Knowledge Base (KB) layer that auto-drafts responses to `QUESTION`-class inbound messages when classifier confidence reaches 90% or above. KB entries are stored in `knowledge_base_entries` and matched against the cleaned message body using trigger patterns. Auto-responses are queued as Slack approval cards before dispatch — they are not sent autonomously until the KB template class has earned 50 clean approvals (Band 2 promotion).

**Business Requirements**

- `knowledge_base_entries` table: `entry_id`, `topic`, `trigger_patterns` (TEXT[]), `approved_response_template` (TEXT), `min_confidence_threshold` (NUMERIC default 0.90), `is_active` (BOOLEAN)
- Matching logic: for a `QUESTION`-class message, the KB engine scores each active entry's `trigger_patterns` against the cleaned message body; selects the highest-confidence match above `min_confidence_threshold`
- Match found + confidence ≥90%: draft auto-response queued as a Slack card in `#sales-replies` for human approval; email NOT sent until Approve clicked (Band 1 behaviour)
- Match found + confidence <90%: `requires_human_review = TRUE` on the `inbound_messages` row; card routed to `#sales-replies` with a "Low confidence — review required" indicator
- No match: `requires_human_review = TRUE`; routed to `#sales-replies` for manual reply
- Seed the KB with minimum 5 active entries covering: "How does this work?", "What does it cost?", "What areas do you cover?", "How long does it take?", "Is this a guarantee?"

**Definition of Done**

- [ ] KB match on "What does it cost?" returns the fee-deflection template (not a fee figure — confirmed by reading the template content, which must not quote any dollar amount and must offer a meeting)
- [ ] Confidence ≥90% match queues approval card in `#sales-replies`; email NOT sent before Approve (confirmed by checking `outbound_touch_dispatched` events — none present before Approve click)
- [ ] Confidence <90% match sets `requires_human_review = TRUE` and shows "Low confidence" indicator on the Slack card
- [ ] No KB match sets `requires_human_review = TRUE` with no draft response generated
- [ ] All 5 seed KB entries present in `knowledge_base_entries` with `is_active = TRUE`; each entry returns a correct match for its target phrase in a test classification run

---

## Developer 3 — Win-Back Recipe, Speed-to-Lead & Skip-Trace Verification

### Task 3.1 — Win-Back Recipe with Assessor/FRBO Match

---

#### Subtask 3.1.1 — Lost-Owner CSV Ingest with Assessor & FRBO Cross-Reference

**Description**
Build the Win-Back ingest pipeline that processes the client's lost-owner CSV, cross-references each owner against the county assessor roll (still owns?) and an FRBO feed (still renting?), and writes a `disposition` column to every row before any outreach sequence fires. Only owners dispositioned `STILL_OWNS_STILL_RENTING` or `STILL_OWNS_NOT_RENTING` enter the outreach sequence; `SOLD` rows are suppressed automatically.

**Business Requirements**

- CSV ingest accepts client's lost-owner file; minimum required columns: owner name, property address, county, last known phone, last known email
- For each CSV row, the pipeline executes two lookups:
  - **Assessor roll check:** query the county tax assessor parcel data by address; if the same owner name still appears on the parcel record → `still_owns = TRUE`; if name changed or parcel not found → `still_owns = FALSE`
  - **FRBO feed check:** query the FRBO listing feed by property address; if an active rental listing exists → `still_renting = TRUE`; if no listing → `still_renting = FALSE`; if feed unavailable → `still_renting = UNKNOWN`
- `disposition` column written to each row:
  - `STILL_OWNS_STILL_RENTING` → proceed to outreach (highest priority)
  - `STILL_OWNS_NOT_RENTING` → proceed to outreach (lower priority)
  - `SOLD` → suppress; do not enter any sequence
  - `UNKNOWN` → hold; do not enter sequence; flag for manual review
- Dispositioned output is exportable as a CSV (client-downloadable from the portal)
- DNC scrub and cross-client non-poach check runs after disposition, before any sequence arm
- `winback_import_completed` event written to `events` with row counts for each disposition bucket

**Definition of Done**

- [ ] Upload a 50-row test CSV; confirm all rows receive a disposition value (no nulls)
- [ ] `SOLD` rows: confirm zero `inbound_messages` or sequence records created for those rows; `suppression_state = TRUE` written on matching contacts
- [ ] `UNKNOWN` rows: confirm `requires_human_review = TRUE`; no sequence armed
- [ ] Assessor match test: a row where the owner name matches the current assessor record gets `still_owns = TRUE`; a row where name differs gets `still_owns = FALSE` (tested with seeded assessor data)
- [ ] Exported CSV contains `disposition` column with correct values for all 50 rows
- [ ] `winback_import_completed` event logged with correct counts for each disposition bucket
- [ ] DNC scrub confirmed to run after disposition (not before); `STILL_OWNS_STILL_RENTING` rows with DNC matches are suppressed before sequence arms

---

#### Subtask 3.1.2 — Three-Touch Win-Back Sequence

**Description**
Build the 3-touch hyper-personalised re-engagement email sequence dispatched from the client's own domain to `STILL_OWNS_STILL_RENTING` and `STILL_OWNS_NOT_RENTING` dispositioned leads. Sequence stops immediately on any reply, opt-out, or booking. Email only — no SMS, no AI voice calls.

**Business Requirements**

- Touch 1 (Market Shift Angle): references the property address and local market conditions; personalised opening hook from the `audit_loss_dollars_est` field if available
- Touch 2 (Ancillary Value): sent Day 5 if no reply to Touch 1; highlights fee lines or services the owner likely isn't capturing under self-management
- Touch 3 (Direct Check-in): sent Day 12 if no reply to Touch 1 or Touch 2; low-pressure direct check-in with a calendar booking link
- All three touches dispatched from the client's own domain (client-dedicated sending cluster, not Blackink's internal pool)
- Sequence stops immediately on: any inbound reply (any sentiment), `is_opted_out = TRUE`, or a `meeting_booked` event for that contact
- Each touch requires human approval in `#blackink-setter` before dispatch (same Band 1 gate as outbound campaign touches in Week 1)
- `outbound_touch_dispatched` event logged for each touch with `campaign_type = 'WIN_BACK'`
- Priority ordering: `STILL_OWNS_STILL_RENTING` contacts are sequenced first; `STILL_OWNS_NOT_RENTING` contacts start the following day

**Definition of Done**

- [ ] Touch 1 draft card appears in `#blackink-setter` for a test `STILL_OWNS_STILL_RENTING` contact; email NOT dispatched until Approve clicked
- [ ] Touch 1 email received in test inbox dispatched from the client-dedicated domain (not a Blackink internal address); confirmed via `From:` header
- [ ] Touch 2 does not fire if test contact replies between Touch 1 and Touch 2 — confirmed by checking that no Touch 2 card is queued after an inbound reply is logged
- [ ] Touch 3 contains a working calendar booking link (Google Calendar / GHL); no SMS instruction in any touch body
- [ ] `STILL_OWNS_NOT_RENTING` Touch 1 cards do not appear in `#blackink-setter` until the day after all `STILL_OWNS_STILL_RENTING` cards have been queued
- [ ] `outbound_touch_dispatched` events logged with `campaign_type = 'WIN_BACK'` for each dispatched touch

---

### Task 3.2 — Skip-Trace Verification

---

#### Subtask 3.2.1 — Enrichment Pipeline Wiring Verification

**Description**
Verify that the owner enrichment step (owner entity → verified phone + email) runs for every signal that enters the Win-Back and Speed-to-Lead pipelines. This includes homestead-drop and same-owner rows when they arrive from the data pipeline (future signals). Without enrichment, Win-Back sequences fire blind and Speed-to-Lead cannot reach owners. This is a verification and wiring task — if the enrichment step is not wired, wire it (estimated 1–2 hours); if it is wired, document the confirmation.

**Business Requirements**

- Run a sample batch of 10 owner signals (from Win-Back ingest) through the enrichment pipeline (Hunter / Anymail / Tracerfy)
- Each signal must exit enrichment with at least one of: verified email, verified phone — logged on the contact record
- Enrichment failures (no result found): `email_status = 'UNVERIFIED'` remains; signal is flagged `requires_enrichment_review = TRUE` and is NOT sequenced
- Confirm enrichment is also wired to homestead-drop signals (can be a stub/no-op if homestead signals are not yet flowing, but the hook must exist in the pipeline code)
- Enrichment result logged to `events` with `event_type = 'owner_enrichment_completed'` and `provider` field
- Verification result (pass/fail per signal) posted to `#blackink-qa` as a summary message before the founding client pilot arms

**Definition of Done**

- [ ] 10 test owner signals run through enrichment; ≥8 of 10 return a verified email or phone (realistic expectation given provider match rates)
- [ ] Failed enrichments (2 of 10) have `email_status = 'UNVERIFIED'` and `requires_enrichment_review = TRUE` — confirmed via SELECT; no sequence record created for those contacts
- [ ] `owner_enrichment_completed` event logged for each signal with `provider` field populated
- [ ] Code review confirms a homestead-drop signal hook exists in the enrichment pipeline (even if the hook body is a no-op pending that signal's arrival)
- [ ] Verification summary posted to `#blackink-qa` showing counts of enriched / failed / pending before founding client pilot arms

---

## Developer 4 — Tenant Infrastructure, Speed-to-Lead & Founding Client Pilot

### Task 4.1 — Tenant-Isolated Sending Reputations

---

#### Subtask 4.1.1 — 20-Domain / 40-Mailbox Sending Architecture & Deliverability Sentinel

**Description**
Deploy the full 20-domain / 40-mailbox tenant-isolated sending architecture. Five domains (10 mailboxes) are allocated to Blackink's internal outbound; 15 domains (30 mailboxes) are reserved for clients, provisioned as 3-domain / 6-mailbox clusters per client. A Deliverability Sentinel monitors all active domains in real time and auto-quarantines any domain that degrades.

**Business Requirements**

- Internal pool (5 domains / 10 mailboxes): `growth-`, `connect-`, `audit-`, `pm-`, `scale-getblackink.com` (2 mailboxes each); SPF, DKIM, DMARC verified on all five
- Client pool: 15 domains allocated in 3-domain / 6-mailbox clusters; first two clusters provisioned for founding clients 1 and 2
- Per-mailbox daily ceiling: 30–50 cold emails/day; sequencer enforces rotation across the 6-mailbox client cluster — no single mailbox exceeds the ceiling in a 24-hour window
- Deliverability Sentinel runs on a 48-hour rolling window per domain: if `bounce_rate > 3%` OR `spam_complaint_rate > 0.08%` → auto-quarantine triggered
- Quarantine procedure: pause outbound dispatch on degraded domain, swap in a pre-warmed reserve domain for active campaigns, post `domain_quarantined` alert to `#blackink-qa` with degradation metrics
- Tenant isolation enforced at DB level: every `outbound_touch_dispatched` event carries `client_id`; cross-tenant dispatch is blocked by the sending orchestrator before any API call
- SPF, DKIM, and DMARC fully configured on all provisioned domains — verified via MXToolbox or equivalent DNS checker

**Definition of Done**

- [ ] All 5 internal domains and 2 client cluster domains provisioned; SPF, DKIM, DMARC verified green on all 7 (confirmed via DNS checker output in `#blackink-qa`)
- [ ] Daily ceiling: attempt to queue 60 cold emails from a single mailbox in 24 hours; confirm only 50 are queued; 10 are deferred to the next rotation slot
- [ ] Tenant A campaigns confirmed to dispatch only from Tenant A's 3-domain cluster — no Tenant B domain appears in any Tenant A `outbound_touch_dispatched` event
- [ ] Deliverability Sentinel: inject a domain with simulated bounce rate of 4% into the monitoring window; confirm `domain_quarantined` alert fires in `#blackink-qa` and a reserve domain is swapped into active campaigns within 5 minutes
- [ ] Cross-tenant isolation test: attempt to dispatch from Tenant A's code path using Tenant B's `client_id` — blocked by orchestrator, `cross_tenant_blocked` event logged

---

**Status (Dev 4 — updated 2026-09-07)**

Code is effectively complete; the DoD above is written against a *live, provisioned, verified*
system, so the task is not "done" until manual provisioning + verification land.

*Code — DONE* (branch `feature/week2-4.1-tenant-sending`, pushed):
- `sending_domains` / `mailboxes` schema, mailbox picker (LRU rotation + rolling-24h cap),
  cross-tenant dispatch block (query + hard assert + RLS), deliverability sentinel — all built.
- Per-client daily send ceiling wired: picker reads `clients.daily_send_ceiling` (0/NULL →
  default 50). `mailbox_dispatcher.py` + tests (38 passed).
- Cluster-scoped reserve swap: sentinel promotes only a same-`cluster_label` + same-`client_id`
  reserve. `deliverability_sentinel.py`.

*Open — NOT done:*
- ⚠ **Reserve model conflict** — client supplied 3 **global** reserves (`equitypulsepartners`,
  `capitalreachhq`, `trusteecompass`), but the swap now requires a same-tenant reserve. Needs a
  client decision: global reserves vs per-cluster reserves (blocks the sentinel swap in the pilot).
- **Provisioning (manual runbook)** — DNS SPF/DKIM/DMARC on all 20 domains, 40 Google Workspace
  mailboxes + SMTP app passwords, warm-up start. Escalated to CEO; on the critical path for Sept 16.
- **Mailbox naming** — client gave domains, not mailbox names. Proposed scheme `firstname@domain`
  (2 per domain); awaiting client confirmation. Flagged in the client report.
- **Pre-pilot verification** — the five DoD tests above require a live DB + provisioned domains;
  not yet run.

---

### Task 4.2 — Speed-to-Lead Recipe with Six-Attempt Cadence & Pay-Per-Lead Routing

---

#### Subtask 4.2.1 — Dual-Path Ingest Orchestrator & 30-Minute Response SLA

**Description**
Build the Speed-to-Lead ingest orchestrator that accepts owner inquiries via two paths — direct webhook (Path A) and email-parsing fallback (Path B) — and delivers a personalised response email with a calendar booking link within 30 minutes during business hours (next business morning after hours). A high-priority alert fires to `#blackink-setter` simultaneously.

**Business Requirements**

- **Path A — Webhook:** endpoint `/api/v1/webhooks/inbound-lead`; processes webhook payload within 2 seconds; validates phone and email fields; checks non-poach gate before proceeding
- **Path B — Email parse:** dedicated inbound address `leads@{client-subdomain}.getblackink.com`; parses prospect name, phone, property address, and inquiry text from notification email bodies via regex; processing within 5 seconds of email receipt
- Both paths write an `inbound_lead_received` event to `events` with `source_channel` field identifying the originating path
- Automated response: personalised email dispatched within 30 minutes business hours (next business morning if after hours); contains client-branded greeting, property management context, and Google Calendar / GHL booking link; email only — no SMS
- High-priority closer alert fired to `#blackink-setter` simultaneously with the response email — same Slack card format as the triage context cards (name, company, inquiry text, local time)
- SLA timer written to the `inbound_messages` row: `sla_due_at = received_at + 30 minutes` during business hours
- Non-poach gate checked before response dispatch; `non_poach_suppressed` event logged if match found — no response sent

**Definition of Done**

- [ ] Path A: POST a test webhook payload to the endpoint; confirm `inbound_lead_received` event written within 2 seconds; response email received in test inbox within 30 minutes
- [ ] Path B: send an email matching a Zillow notification format to the `leads@` address; confirm prospect data extracted correctly from the body; response dispatched within 30 minutes
- [ ] After-hours test: webhook received at 9 PM ET; confirm response is queued for next business morning (not dispatched at 9 PM)
- [ ] Closer alert card appears in `#blackink-setter` within 60 seconds of lead receipt for both Path A and Path B
- [ ] Non-poach test: submit an inquiry from an email address in an active client's PM book; confirm no response dispatched; `non_poach_suppressed` event logged
- [ ] No SMS dispatch in any Speed-to-Lead code path — confirmed via code review

---

#### Subtask 4.2.2 — Six-Attempt Inbound Cadence

**Description**
After the initial 60-second acknowledgement fires, if the owner goes quiet (no reply, no booking within 24 hours), arm a 5-further-attempt follow-up cadence over 5 days via GHL sequence. The cadence stops immediately on any reply, opt-out, or booking. This runs in the same orchestrator code path as the Speed-to-Lead response — not a separate system.

**Business Requirements**

- Cadence trigger: if `inbound_lead_received` event exists for a contact and no `inbound_reply_received`, `meeting_booked`, or `is_opted_out = TRUE` event exists within 24 hours → arm the cadence
- 5 follow-up attempts over 5 days: Day 1, Day 2, Day 3, Day 4, Day 5 (one per day); each attempt is an email touch dispatched via GHL sequence from the client's configured domain
- Cadence stops on ANY of: inbound reply of any sentiment, `meeting_booked` event, `is_opted_out = TRUE` on the contact row
- Each cadence touch logged to `events` with `event_type = 'outbound_touch_dispatched'` and `campaign_type = 'SPEED_TO_LEAD_CADENCE'` and `touch_step` (1–5)
- GHL sequence ID stored as a config row per client — not hardcoded; sequence is armed via the GHL API on cadence trigger
- Human approval NOT required for cadence touches (these are follow-ups to an inbound inquiry, not cold outbound); follows the Lead Agent autonomy model

**Definition of Done**

- [ ] Cadence arms for a test contact 24 hours after lead receipt with no reply — confirmed via job queue showing 5 scheduled follow-up touches
- [ ] Touch 1 (Day 1) dispatched from the client's configured domain; `campaign_type = 'SPEED_TO_LEAD_CADENCE'` confirmed in the `events` row
- [ ] Cadence stops when a test reply is logged between Touch 2 and Touch 3 — Touch 3 through 5 jobs are cancelled (confirmed via job queue inspection showing removed entries)
- [ ] Cadence stops when `is_opted_out = TRUE` is set at any point — confirmed via contact record check
- [ ] GHL sequence ID confirmed read from a config row, not a hardcoded constant — confirmed via code review
- [ ] No SMS dispatch in any cadence touch — confirmed via code review

---

#### Subtask 4.2.3 — Pay-Per-Lead Routing (APM, Manage My Property, Thumbtack)

**Description**
Extend the Speed-to-Lead Path B email-parsing fallback to recognise and parse inbound lead notifications from three pay-per-lead portals: All Property Management (APM), Manage My Property, and Thumbtack. Each portal sends a notification email in a distinct format. Parsed leads are queued into the Rhow espond queue with a `source_channel` tag identifying the originating portal.

**Business Requirements**

- Three parser modules added to the Path B email-parsing engine:
  - `APM` parser: matches notification emails from All Property Management; extracts owner name, property address, phone, and inquiry text using APM's email format
  - `MANAGE_MY_PROPERTY` parser: matches Manage My Property notification emails; same extraction fields
  - `THUMBTACK` parser: matches Thumbtack lead notification emails; extracts name, job description, budget (if present), and contact details
- `source_channel` written to `inbound_messages` row: one of `APM`, `MANAGE_MY_PROPERTY`, `THUMBTACK`, `WEBSITE_FORM`, `LISTING_PORTAL` (existing values unchanged)
- All three portal leads enter the same Respond queue and response SLA (30 minutes business hours) as native website inquiries
- Non-poach gate and DNC check run before any response dispatch — same gate as all other Speed-to-Lead paths
- Parser modules are config-driven: each portal's email sender address and subject line pattern stored in a config row; adding a new portal requires only a new config row and a parser function — no core orchestrator changes

**Definition of Done**

- [ ] Send a test APM-format notification email to the `leads@` address; confirm prospect data extracted correctly; `source_channel = 'APM'` written to `inbound_messages`; response dispatched within 30 minutes
- [ ] Repeat for Manage My Property format — correct extraction and `source_channel = 'MANAGE_MY_PROPERTY'` confirmed
- [ ] Repeat for Thumbtack format — correct extraction and `source_channel = 'THUMBTACK'` confirmed
- [ ] An unrecognised portal email falls through to an `UNCLASSIFIED` bucket with `requires_human_review = TRUE` — no crash, no silent drop
- [ ] Config row structure confirmed: adding a 4th portal requires only a new config row + parser function with no changes to the orchestrator (confirmed via code review)
- [ ] Non-poach gate confirmed to run on all three portal paths — `non_poach_suppressed` event fires correctly for a test match

---

### Task 4.3 — Founding Client Pilot Coordination (September 16–18)

---

#### Subtask 4.3.1 — Pilot Tenant Provisioning & Six-Stage Operational Runbook

**Description**
Execute the six-stage founding client pilot for clients #1 and #2. This is an operational coordination task — the underlying systems are built by the other three developers this sprint; Developer 4 owns the provisioning scripts, runbook execution, and live monitoring during the pilot window. Engineering hand-holding is explicitly permitted.

**Business Requirements**

- Stage 1 — Tenant Clone: run `clone_tenant_environment.sh` for client #1; provision isolated `client_id`; assign 3-domain / 6-mailbox cluster from the provisioned pool; confirm zero credential leakage between tenant workspaces
- Stage 2 — Historical Ingest: upload client #1's dead-lead CSV; run assessor/FRBO match pipeline (Subtask 3.1.1); confirm disposition column populated; run DNC scrub; confirm `SOLD` rows suppressed
- Stage 3 — Compliance Guard: execute cross-client non-poach check; verify opt-out tables; validate county-specific eligibility; confirm skip-trace enrichment verification has been logged to `#blackink-qa`
- Stage 4 — Campaign Launch: arm Win-Back recipe (assessor-matched) and Speed-to-Lead recipe (with six-attempt cadence); dispatch initial batch from client #1's dedicated mailboxes; confirm tracking webhooks active
- Stage 5 — Triage & Routing: ingest live prospect replies during the pilot window; confirm intent classification runs; hot leads appear in `#blackink-setter` within 60 seconds
- Stage 6 — Booking & Dashboards: complete a live meeting booking; verify `meeting_booked` event created; confirm appointment state machine transitions (`BOOKED → CONFIRMED_24H`); confirm Client Wins Dashboard reflects live metrics
- Repeat all six stages for client #2 following the same runbook; document any friction points encountered during client #1 provisioning

**Definition of Done**

- [ ] Client #1 tenant workspace live with isolated `client_id`; zero credential leakage confirmed (cross-tenant isolation test passes)
- [ ] Client #1 dead-lead CSV dispositioned; `disposition` column populated; `SOLD` rows suppressed; DNC scrub completed; enrichment verification logged to `#blackink-qa`
- [ ] Client #1 Win-Back and Speed-to-Lead campaigns active; at least 1 outbound email dispatched from client #1's dedicated domain
- [ ] At least 1 live reply received and classified by the triage agent; context card appears in `#blackink-setter` within 60 seconds
- [ ] At least 1 live meeting booking completed; `meeting_booked` event confirmed in `events`; appointment state machine shows `CONFIRMED_24H` after confirmation email dispatch
- [ ] Client Wins Dashboard loads with live data for client #1 (not empty panels; not sandbox data)
- [ ] Client #2 pilot started using the same runbook; friction points from client #1 documented and shared with the team before client #2 provisioning begins

---

## Week 2 — Shared Definition of Done (Gate: September 18, 2026)

All four developers must collectively demonstrate the following live on September 18:

1. **Reply Classification** — Process a test batch of 50 inbound replies across all 10 intent classes; demonstrate ≥80% accuracy; show `HOT_LEAD` halting a sequence and generating a context card in `#blackink-setter`; show `UNSUBSCRIBE` writing `is_opted_out = TRUE` without any LLM call.
2. **Appointment State Machine** — Advance a test appointment through all states; demonstrate `is_billable` evaluating correctly under all combinations; show a third reschedule auto-marking `LOST`; show `opportunity_id` preserved across reschedule.
3. **Settlement Execution + Billing Rules** — Execute a full settlement flow in Stripe test mode: zero dollars charged upfront, ACH mandate stored, Evidence Packet PDF generated, 60-day clawback job scheduled. Demonstrate: $50 miss credit written on a slow ack, first-sit-free on a founding account's first appointment, founding flag protecting a client's price from a rate migration.
4. **Tenant-Isolated Sending** — Show Tenant A campaigns dispatching exclusively from Tenant A's domain cluster; show the Deliverability Sentinel firing a quarantine alert when a domain crosses the 3% bounce threshold.
5. **Speed-to-Lead + Cadence + Pay-Per-Lead** — Trigger a lead via webhook (Path A) and via the `leads@` parse address (Path B); verify response dispatched within 30 minutes; show a six-attempt cadence arming when the owner goes quiet; show an APM-format notification email parsed and queued in the Respond queue with `source_channel = 'APM'`.
6. **Win-Back Import** — Upload a 50-row lost-owner CSV; show disposition column populated with assessor/FRBO match results; show `SOLD` rows suppressed; show only `STILL_OWNS_STILL_RENTING` contacts receiving Touch 1 draft cards in `#blackink-setter`.
7. **Skip-Trace Verification** — Show enrichment pipeline result summary posted to `#blackink-qa`; at least 8 of 10 test signals return a verified contact method; failed signals blocked from sequencing.
8. **Founding Client Pilot Live** — Client #1 tenant active with Win-Back and Speed-to-Lead campaigns dispatching; at least one live reply classified and routed; at least one live booking created; Client Wins Dashboard showing real client #1 data.
