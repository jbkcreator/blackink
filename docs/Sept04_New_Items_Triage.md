# Sept 04 Client Comments — New Items Triage

| | |
|---|---|
| **Source** | `client_comments_2026-09-04.md` |
| **Total Items** | 34 |
| **Last Updated** | 2026-09-07 |

> All 34 items originate from the Sept 04 client comments. None existed in the blueprint before that date.
> Week assignments marked *(assumed)* are inferred from logical sprint sequencing — confirm with team before locking.

---

## Contents

1. [Pricing & Account Rules](#1-pricing--account-rules) — 6 items, all Week 2
2. [Billing Rules §6b](#2-billing-rules-6b) — 5 items, all Week 2
3. [Signals & Sourcing](#3-signals--sourcing) — 6 items, Weeks 2–4
4. [Inbound & Reactivation](#4-inbound--reactivation) — 5 items, Weeks 2–4 + 1 blocked
5. [Referral Partner System](#5-referral-partner-system) — 7 items, all Week 3
6. [Owner-Facing & Demo](#6-owner-facing--demo) — 5 items, Weeks 2–3 + 1 deferred
7. [Summary Index](#7-summary-index)

---

## 1. Pricing & Account Rules

---

### Three Founding SKUs

**Week:** 2

**Description**
Replace all previous pricing tiers with three founding-rate SKUs. Every row on the offer sheet prints "Founding rate — normally higher." No list price stored or displayed anywhere.

| SKU | Price | Notes |
|---|---|---|
| Respond | $397/mo | Founding rate |
| Owner Growth | $749/mo + $99/sit | Founding rate. $897 flat stays as a non-default row |
| Full County | $1,197/mo + $99/sit | Founding rate. Exclusive — one per county |

**Notes**
Retired rows: $297 Respond, Scorecard $449 tier, additional-county add-on.

**Definition of Done**
- [ ] Three SKU rows present in `entitlement_offers` with correct prices
- [ ] $897 flat row present and marked non-default
- [ ] Offer sheet displays "Founding rate — normally higher" on every row
- [ ] Retired rows removed from all pricing surfaces
- [ ] Demo kit config reflects $397 / $749+$99 / $1,197+$99 (not $397+$100)

---

### `founding = true` Flag on Account Row

**Week:** 2

**Description**
Add a `founding BOOLEAN DEFAULT FALSE` column to the `companies` table. The billing job checks this flag before any pricing migration — founding accounts never move when list prices change later. Set to `TRUE` at account creation for all September founding clients.

**Notes**
One flag, one check; no per-invoice override.

**Definition of Done**
- [ ] `founding` column present on `companies` table with `DEFAULT FALSE`
- [ ] Billing job contains a `WHERE founding = TRUE` predicate that skips price migration
- [ ] Create a founding client; run a pricing migration; confirm founding account price unchanged while a non-founding account updates
- [ ] All September founding clients provisioned with `founding = TRUE`

---

### No List Price Stored or Shown Anywhere

**Week:** 2

**Description**
No dollar amount from the pricing catalogue may be stored in a visible field or displayed on any client-facing surface. The offer sheet shows the founding-rate label only. This is a config rule enforced at the data and template layer, not a UI feature.

**Notes**
Applies to all screens, emails, and exports.

**Definition of Done**
- [ ] Code review confirms no hardcoded list price appears in any template, email body, or UI component
- [ ] Database audit confirms no `list_price` column stores a dollar figure visible to clients
- [ ] Offer sheet renders "Founding rate — normally higher" without a comparative dollar amount

---

### Full County Exclusivity Lock in Allocator

**Week:** 2

**Description**
The county allocator enforces one `full_county` flag per county. A second Full County assignment to the same county is rejected at the allocator level before any DB write. Owner Growth accounts in a Full County county retain their ZIPs until renewal.

**Notes**
Enforcement is in the allocator layer, not application logic.

**Definition of Done**
- [ ] `full_county` flag present on the county entitlement row
- [ ] Attempt to assign Full County to a county that already has a Full County client — allocator rejects the assignment, no DB write occurs
- [ ] Owner Growth accounts in a Full County county confirm their ZIP assignments are unaffected
- [ ] `full_county_conflict_rejected` event logged on attempted second assignment

---

### No Monthly Ceiling on Billable Attended Sits

**Week:** 2

**Description**
Remove any monthly cap on the number of billable attended sits. The billing gate has no `COUNT(*) >= cap` check. Clients with high volume bill for every qualifying sit without ceiling.

**Notes**
If a cap exists anywhere in the billing gate, remove it.

**Definition of Done**
- [ ] `monthly_cap = NULL` confirmed on `appt_standard` and `appt_first` entitlement rows via SELECT
- [ ] Submit 50 attended sits for a test client in one calendar month — all 50 are billed without any block
- [ ] Code review confirms no `COUNT(*) >= cap` predicate exists in the billing job

---

### Demo Kit Config Correction

**Week:** 2

**Description**
Update the demo kit configuration to display correct founding-rate pricing. Previous config showed $397+$100/sit which is incorrect.

**Definition of Done**
- [ ] Demo kit renders $397 for Respond, $749+$99 for Owner Growth, $1,197+$99 for Full County
- [ ] No instance of "$397+$100" or "$100/sit" appears on any demo screen

---

## 2. Billing Rules §6b

---

### $50 Miss Credit

**Week:** 2

**Description**
Any Respond inbound email not acknowledged inside 60 seconds automatically writes a $50 credit line to the client's next invoice. No human approval required. Both the miss event and the credit are visible on the proof ledger.

**Notes**
Trigger: `ack_latency_seconds > 60` on `inbound_messages` where `channel = 'EMAIL'` and intent not yet classified.

**Definition of Done**
- [ ] Trigger an inbound message with `ack_latency_seconds = 90`; confirm $50 credit line appears on the client's next invoice automatically
- [ ] Miss event and credit line both visible on the proof ledger
- [ ] No human approval step exists in the code path — confirmed via code review
- [ ] Inbound acknowledged in 45 seconds — no credit written

---

### First Sit Free

**Week:** 2

**Description**
The first `attended = TRUE` sit per account bills at $0. Enforced via a `first_sit_consumed BOOLEAN DEFAULT FALSE` flag on the client entitlement row. The billing job checks the flag before charging; on $0 charge it flips the flag to `TRUE`. Cannot be overridden per-invoice.

**Definition of Done**
- [ ] First attended appointment for a test account charges $0 in Stripe test mode
- [ ] `first_sit_consumed` flips to `TRUE` after the first sit
- [ ] Second attended appointment charges $99 (confirmed in Stripe test mode)
- [ ] No per-invoice override path exists — confirmed via code review

---

### 60-Day Guarantee

**Week:** 2

**Description**
If an Owner Growth or Full County account has fewer than four attended qualified sits at day 60, the next month's subscription bills at $0. One-time per account. A `guarantee_applied BOOLEAN DEFAULT FALSE` flag prevents repeat application.

**Notes**
Scheduled job at day 60 counts qualifying sits and applies the override.

**Definition of Done**
- [ ] Simulate a day-60 check with 3 qualified sits — next month subscription writes as $0; `guarantee_applied = TRUE`
- [ ] Simulate a day-60 check with 5 qualified sits — no override applied
- [ ] `guarantee_applied = TRUE` prevents a second override on the same account
- [ ] Only Owner Growth and Full County accounts are eligible — Respond accounts excluded

---

### Dispute Credited on Flagging, Not on Resolution

**Week:** 2

**Description**
A disputed appointment sit is credited at the moment the dispute is flagged (`flagged_at`), not when it is resolved. The dispute window is 48 hours from the scheduled meeting time. `appointment_disputes.outcome` defaults to `CREDITED_AUTOMATIC`.

**Notes**
Also corrects the Week 4 dispute window — changed from "5-business-day" to "48-hour window from the scheduled meeting time."

**Definition of Done**
- [ ] Insert a dispute row; confirm credit line written immediately at `flagged_at` timestamp
- [ ] Confirm no second credit written at `resolved_at`
- [ ] `outcome` column defaults to `CREDITED_AUTOMATIC` — confirmed via `\d+ appointment_disputes`
- [ ] Dispute window test: flag a dispute 49 hours after the scheduled meeting time — system rejects the flag as outside the window

---

### Skip-Trace Confirmation on Surfaced Owners

**Week:** 2

**Description**
Verify that the enrichment step (owner entity → verified phone + email) runs for every signal entering the Win-Back and Speed-to-Lead pipelines. If the step is not wired, wire it (estimated 1–2 hrs). This is a verification task and a pilot-launch blocker.

**Notes**
Must cover homestead-drop and same-owner rows when those signals arrive — stub hook is acceptable for September.

**Definition of Done**
- [ ] 10 test owner signals processed through enrichment; ≥8 return a verified email or phone
- [ ] Failed enrichments: `email_status = 'UNVERIFIED'` and `requires_enrichment_review = TRUE` — no sequence created
- [ ] `owner_enrichment_completed` event logged for each signal with `provider` field
- [ ] Homestead-drop signal hook exists in enrichment pipeline code (stub acceptable)
- [ ] Verification summary posted to `#blackink-qa` before founding client pilot arms

---

## 3. Signals & Sourcing

---

### Win-Back Import Assessor/FRBO Match

**Week:** 2

**Description**
Extend the Win-Back dead-lead CSV ingest to cross-reference each owner against the county assessor roll (still owns?) and an FRBO listing feed (still renting?). Write a `disposition` column to every row before any outreach sequence arms.

| Disposition | Action |
|---|---|
| `STILL_OWNS_STILL_RENTING` | Proceed to outreach — highest priority |
| `STILL_OWNS_NOT_RENTING` | Proceed to outreach — lower priority |
| `SOLD` | Suppress — do not enter any sequence |
| `UNKNOWN` | Hold — flag for manual review |

**Notes**
Assessor and FRBO lookups run per row before any sequence arms.

**Definition of Done**
- [ ] Upload a 50-row test CSV; confirm all rows receive a disposition value (no nulls)
- [ ] `SOLD` rows: zero sequence records created; `suppression_state = TRUE` on matching contacts
- [ ] `UNKNOWN` rows: `requires_human_review = TRUE`; no sequence armed
- [ ] Assessor match: owner name matches current record → `still_owns = TRUE`; name differs → `still_owns = FALSE`
- [ ] Exported CSV contains `disposition` column with correct values for all rows
- [ ] `winback_import_completed` event logged with counts for each disposition bucket

---

### Homestead Exemption Dropped as First-Class Signal

**Week:** 3 *(assumed)*

**Description**
When the county assessor roll shows a homestead exemption has been dropped for a property (owner moved out, property stayed as a rental), treat it as a first-class distress signal with its own row in the Owner Packet — not bundled under a generic flag. The signal feeds the outbound Grow pipeline.

**Notes**
Fits with Owner Packet / Prospecting Agent expansion work. 2–3 hours estimated. Cannot be surfaced until the Owner Packet screen is being actively built.

**Definition of Done**
- [ ] `homestead_dropped` signal type present as a distinct row in the Owner Packet
- [ ] Signal fires when assessor data shows exemption removed; `signal_type = 'HOMESTEAD_DROPPED'` written to signals table
- [ ] Signal ranked correctly in the distress signal priority order (confirm rank position with client)
- [ ] `signal_surfaced_to` event logged when the signal is shown to a referral partner or setter

---

### Same-Owner Assessor Match

**Week:** 4 *(assumed)*

**Description**
Accept the client's managed-address list (CSV or PMS export — address and owner name). Group the county assessor roll by owner mailing address and entity. Surface parcels those owners hold that are not on the client's managed list. Output: up to 12 addresses per run, exportable, shown on Screen 1. Identifies expansion opportunities within existing owner relationships.

**Notes**
Largest new item at 12–20 hours. Requires a separate data pipeline: client CSV ingest, assessor roll group-by join, deduplication against managed list, and a ranked output. Too large for Week 2 or 3 given pilot and retention work.

**Definition of Done**
- [ ] Client uploads a managed-address CSV; pipeline cross-references against assessor roll
- [ ] Output contains up to 12 unmanaged parcels owned by the same owners, ranked by parcel count
- [ ] Output is exportable as a CSV from the portal
- [ ] Output is visible on Screen 1 under the correct section heading
- [ ] Run does not surface parcels already on the client's managed list

---

### Whale Ranking + Out-of-State Tier

**Week:** 3 *(assumed)*

**Description**
Add parcel count to every owner row in the prospecting sweep. Owners whose mailing address state is not FL receive a priority-tier flag. Sort order in the nightly sweep and the Owner Packet reflects parcel count (descending) with out-of-state owners surfaced first within each tier.

**Notes**
Scoring engine enhancement that fits naturally with Prospecting Agent expansion. 4–6 hours estimated.

**Definition of Done**
- [ ] `parcel_count` field populated on every owner row from the assessor roll
- [ ] `out_of_state_owner` flag set where `mailing_state != 'FL'`
- [ ] Nightly sweep sort order: out-of-state owners appear before in-state owners at equal parcel count
- [ ] Owner Packet reflects parcel count and out-of-state flag in the priority display
- [ ] Whale owners (parcel count ≥ threshold — confirm threshold with client) tagged distinctly from standard owners

---

### Direct Mail Merge

**Week:** 3 *(assumed)*

**Description**
For absentee-owner rows (mailing address ≠ property address), generate a batch print job via Lob or PostGrid. Each piece includes the rent estimate for that property and the client's name. Batch processing only — not per-row on-demand.

**Notes**
Not a founding pilot blocker; straightforward API integration. 4–6 hours estimated. Client instruction: pick one provider (Lob or PostGrid), do not evaluate both.

**Definition of Done**
- [ ] Absentee-owner rows identified correctly (mailing ≠ property address) in the nightly sweep output
- [ ] Batch print job submitted to chosen provider with rent estimate and client name in each piece
- [ ] Provider choice (Lob or PostGrid) documented with rationale
- [ ] Batch job triggered from the portal; not per-row manual trigger
- [ ] Print job ID and status logged to `events` with `event_type = 'direct_mail_dispatched'`
- [ ] Test batch of 5 addresses submitted; provider dashboard confirms receipt

---

### HOA Rental-Cap Flag

**Week:** 4 *(assumed)*

**Description**
Cross-reference condo and HOA property lists for rental caps and waiting periods. Flag properties where the HOA restricts rentals or has an active waiting period before a management appointment can be booked. Prevents booking appointments for properties the client cannot legally manage.

**Notes**
Third-party data source cross-reference; not a September pilot blocker. Hours not specified by client — confirm data source before estimating.

**Definition of Done**
- [ ] HOA rental-cap data source identified and integrated (confirm source with client before building)
- [ ] `hoa_rental_cap` flag set on matching property rows
- [ ] Booking flow blocks appointment creation for flagged properties with a clear reason message
- [ ] Flag visible on the Owner Packet for the property
- [ ] `hoa_cap_block` event logged when a booking attempt is blocked by the flag

---

## 4. Inbound & Reactivation

---

### Six-Attempt Inbound Cadence

**Week:** 2

**Description**
After the initial 60-second acknowledgement email fires for a Speed-to-Lead inbound, if the owner goes quiet (no reply, no booking within 24 hours), arm a 5-further-attempt GHL sequence over 5 days (one per day). Sequence stops immediately on any reply, opt-out, or booking.

**Notes**
Same orchestrator code path as the Speed-to-Lead response — not a separate system. No human approval required for cadence touches; these are follow-ups to an inbound inquiry, not cold outbound.

**Definition of Done**
- [ ] Cadence arms for a test contact 24 hours after lead receipt with no reply — 5 scheduled follow-up touches confirmed in job queue
- [ ] Touch 1 dispatched from client's configured domain; `campaign_type = 'SPEED_TO_LEAD_CADENCE'` in events row
- [ ] Cadence stops when a reply is logged between Touch 2 and Touch 3 — Touch 3–5 jobs cancelled
- [ ] Cadence stops when `is_opted_out = TRUE` set at any point
- [ ] GHL sequence ID read from a config row, not hardcoded — confirmed via code review
- [ ] No SMS dispatch in any cadence touch — confirmed via code review

---

### Pay-Per-Lead Routing

**Week:** 2

**Description**
Extend the Speed-to-Lead Path B email-parsing fallback to parse inbound lead notifications from All Property Management, Manage My Property, and Thumbtack. Parsed leads enter the Respond queue with a `source_channel` tag and receive the same 30-minute response SLA as all other Speed-to-Lead inbounds.

**Notes**
Parser modules are config-driven — adding a new portal requires a config row and a parser function, not a core orchestrator change.

**Definition of Done**
- [ ] APM-format notification email parsed correctly; `source_channel = 'APM'` written; response dispatched within 30 minutes
- [ ] Manage My Property format parsed correctly; `source_channel = 'MANAGE_MY_PROPERTY'` confirmed
- [ ] Thumbtack format parsed correctly; `source_channel = 'THUMBTACK'` confirmed
- [ ] Unrecognised portal email falls through to `UNCLASSIFIED` with `requires_human_review = TRUE`; no crash, no silent drop
- [ ] Adding a 4th portal requires only a new config row + parser function with no orchestrator changes — confirmed via code review
- [ ] Non-poach gate runs on all three portal paths

---

### Post-Sit Nurture Sequence

**Week:** 3 *(assumed)*

**Description**
Appointments dispositioned as "thinking" or "not now" enter a 45-day re-engagement email sequence in the client's name: touches at day 2, 7, 14, 30, and 45. Sequence stops on any reply. A setter task fires automatically at day 14 and day 45 to prompt human follow-up.

**Notes**
Requires the `appointment_dispositions` table built in Week 2. Cannot be wired until the `DISPOSITIONED` state and outcome values exist. 8–12 hours estimated.

**Definition of Done**
- [ ] Appointment dispositioned as `DECIDING` or `NOT_A_FIT` triggers sequence arm (confirm exact outcome values map to "thinking" / "not now")
- [ ] Touches fire at day 2, 7, 14, 30, 45 from the client's configured domain
- [ ] Sequence stops on any inbound reply — confirmed by checking no further touches queued after a test reply
- [ ] Setter task created at day 14 and day 45 with contact context and last disposition note
- [ ] `campaign_type = 'POST_SIT_NURTURE'` on all `outbound_touch_dispatched` events in this sequence

---

### Review-Velocity Trigger

**Week:** 4 *(assumed)*

**Description**
Send a Google review request to a managed owner at two points: at onboarding and at the 90-day mark. Request routes to the client's Google Business Profile (GBP). Email only — no SMS.

**Notes**
The day-90 trigger fires well after the founding client pilot. Onboarding trigger could be wired in Week 3, but no clients will hit day 90 in September, making the full feature Week 4 work.

**Definition of Done**
- [ ] Onboarding trigger fires a review request email within 24 hours of client activation
- [ ] Day-90 scheduled job fires correctly for a test account (simulated via time fast-forward)
- [ ] Review request routes to client's GBP URL (stored in client config, not hardcoded)
- [ ] No SMS sent in either trigger path — confirmed via code review
- [ ] `review_request_sent` event logged with `trigger_type = 'ONBOARDING'` or `'DAY_90'`

---

### Dead-Lead 9-Touch SMS Drip

**Week:** ⛔ BLOCKED

> **Do not build.** Sept 01 comments establish "no outbound SMS this year." This item directly contradicts that. CI must fail on any cold SMS build attempt per existing constraint. Do not build until the client resolves this conflict in writing. 10DLC clearance is a hard prerequisite if approved.

**Description**
Nine SMS touches over 30 days to the client's dead-lead file. Sent from the client's 10DLC number in the client's name, referencing the owner's original inquiry. Fires only where `phone_consent = TRUE`. Quiet hours 8am–8pm ET enforced. STOP propagates to the consent ledger and halts every sequence. Any reply exits the drip to a human.

**Definition of Done**
- [ ] Client resolves the SMS conflict in writing before any build begins
- [ ] 10DLC registration confirmed approved
- [ ] `phone_consent = TRUE` gate enforced — no SMS sent to contacts without explicit consent
- [ ] STOP keyword propagates to consent ledger and halts all sequences for that contact
- [ ] Quiet hours 8am–8pm ET enforced; messages outside this window are deferred, not dropped
- [ ] Any reply exits the drip and routes to a human within 15 minutes

---

## 5. Referral Partner System

> All seven items are new. Build order within Week 3: schema first (`referral_partner` table → `signal_surfaced_to` event → `sell_intent` disposition) before routing and digest layers.

---

### `referral_partner` Table

**Week:** 3 *(assumed)*

**Description**
Create the `referral_partner` table, reusing the vendor table shape. Foundation for the entire referral partner system.

| Field | Type | Notes |
|---|---|---|
| `type` | ENUM | agent / attorney / insurance / lender / CPA / HOA-CAM / trade / other |
| `ZIPs` | TEXT[] | ZIP coverage array |
| `criteria_card` | JSON | Criteria filter card |
| `tier` | ENUM | live / public-record |
| `last_sent` | TIMESTAMPTZ | |
| `last_report_back` | TIMESTAMPTZ | |

**Notes**
3–4 hours estimated. Builds naturally after the pilot launch and Week 2 signal infrastructure is proven.

**Definition of Done**
- [ ] `referral_partner` table created with all specified fields and correct types
- [ ] `type` CHECK constraint enforces the eight allowed values
- [ ] `tier` CHECK constraint enforces `'live'` and `'public-record'` values only
- [ ] `ZIPs` stored as a TEXT array; query returns correct partners for a given ZIP
- [ ] Seed at least one test partner of each type to confirm schema works end to end

---

### `sell_intent` Disposition

**Week:** 3 *(assumed)*

**Description**
Add a `sell_intent` outcome value to the appointment disposition set. Out-of-criteria seller signals — owners who indicate they intend to sell — are tagged with this disposition and remain in the pool. They are not closed or suppressed. This feeds referral partner ZIP routing.

**Notes**
Small addition (2–3 hrs). Requires the `appointment_dispositions` table from Week 2.

**Definition of Done**
- [ ] `sell_intent` present as a valid value in the disposition outcome CHECK constraint
- [ ] Dispositioning an appointment as `sell_intent` does not suppress the contact
- [ ] Contact with `sell_intent` disposition appears in the referral partner routing pool
- [ ] `signal_surfaced_to` event triggered when a `sell_intent` contact is routed to a partner

---

### `signal_surfaced_to` Event

**Week:** 3 *(assumed)*

**Description**
When any signal is shown to a referral partner, record a `signal_surfaced_to` event with the partner ID, signal type, and timestamp. This is a compliance requirement under Florida §475 — the event records that the signal was surfaced, not that an introduction occurred. An `introduction_made` event must never be created.

**Notes**
Must be built before ZIP routing fires any partner sends. FL §475 constraint is non-negotiable. 2–3 hours estimated. Event must be immutable.

**Definition of Done**
- [ ] `signal_surfaced_to` event logged with `partner_id`, `signal_type`, `signal_id`, and `surfaced_at` timestamp
- [ ] No `introduction_made` event exists anywhere in the codebase — confirmed via grep
- [ ] Event fires on both live-tier (same-day) and public-record-tier (weekly batch) partner sends
- [ ] Event is immutable after creation — no UPDATE or DELETE path exists for compliance records

---

### ZIP-Match Routing for Partner Signals

**Week:** 3 *(assumed)*

**Description**
Seller signals and `sell_intent` dispositions are automatically routed to referral partners whose ZIP coverage and criteria card match the property. Live-tier partners receive signals the same day, individually. Public-record-tier partners receive a weekly batch.

**Notes**
Depends on `referral_partner` table and `signal_surfaced_to` event existing first. 4–6 hours estimated.

**Definition of Done**
- [ ] A `sell_intent` signal for ZIP 33601 routes only to partners whose ZIP array includes 33601
- [ ] Criteria card filtering: a partner typed `"agent"` does not receive signals tagged for `"lender"` criteria
- [ ] Live-tier partner receives signal routing the same day
- [ ] Public-record-tier partner receives signals in the next weekly batch (not same-day)
- [ ] `signal_surfaced_to` event logged for every routed signal

---

### Monday Partner Digest

**Week:** 3 *(assumed)*

**Description**
A digest email assembled each Sunday night for each active referral partner, containing 5–8 ranked signal rows. The email is drafted to the client's inbox for approval before any send. It never auto-sends. One email per partner per week.

**Notes**
Depends on ZIP routing being live. 4–6 hours estimated.

**Definition of Done**
- [ ] Sunday night job assembles the digest for each partner with active signals
- [ ] Digest draft appears in **client** inbox for approval — not in partner inbox
- [ ] Digest contains 5–8 rows ranked by signal priority (confirm ranking logic with client)
- [ ] Partners with no new signals this week do not receive a digest
- [ ] No auto-send path exists in the code — client approval required before dispatch

---

### 48-Hour Report-Back

**Week:** 3 *(assumed)*

**Description**
48 hours after any live-tier partner send, trigger a report-back prompt. The partner fills in a one-line status field on the corresponding `signal_surfaced_to` event. If the field is empty after 48 hours, a reminder is sent to the client — not to the partner.

**Notes**
Small addition (2–3 hrs). Depends on partner send events existing.

**Definition of Done**
- [ ] 48-hour scheduled job fires after every live-tier partner send
- [ ] Report-back field on `signal_surfaced_to` event accepts a one-line status string
- [ ] Empty field at 48 hours triggers a reminder to the client (`#client-growth` or configured channel)
- [ ] Reminder goes to the client only — no automated message sent directly to the partner

---

### Partner Scoreboard

**Week:** 3 *(assumed)*

**Description**
A per-partner reporting view showing: signals sent, contacts made, deals closed, junk signals. Accessible as a data export for September. A UI screen is acceptable later; export is sufficient for the founding pilot window.

**Notes**
Reporting layer; can be built last within the referral system. 2–3 hours estimated. Export-first explicitly accepted by client.

**Definition of Done**
- [ ] Export contains per-partner rows with sent, contacted, closed, junk counts
- [ ] Export is accessible from the portal without engineering involvement
- [ ] Counts are accurate against the `signal_surfaced_to` event log and report-back status fields
- [ ] Export covers a configurable date range

---

## 6. Owner-Facing & Demo

---

### Live Inbound Trigger in the Demo

**Week:** 2

**Description**
Screen 5 of the demo has a button that fires a real form-fill or call to the client's own listing line while screen-sharing. The response time is logged to the proof ledger immediately so the prospect can see the speed-to-lead system respond in real time during the demo.

**Notes**
Founding pilot demo requirement; must be live before Sept 16. 1–2 hours estimated.

**Definition of Done**
- [ ] Screen 5 button triggers a real form-fill or inbound call to the client's listing line
- [ ] Response time logged to the proof ledger and visible on-screen within 30 seconds
- [ ] Demo trigger does not create a live sequence for the test contact — fires the ack only
- [ ] Works in a screen-sharing context without browser security prompts blocking the trigger

---

### Pixel + UTM Plumbing

**Week:** 2

**Description**
Install Meta and Google pixels on every client-facing page. Populate `raw_first_source` from UTM parameters on every inbound lead record. Required from day one of the pilot so the first leads generated during the founding client launch are attributed correctly to their source.

**Notes**
Needed before Sept 16 pilot launch; tracking cannot be retrofitted cleanly after leads start arriving. 3–5 hours estimated.

**Definition of Done**
- [ ] Meta pixel fires on page load for all client-facing pages (confirmed via Meta Events Manager)
- [ ] Google pixel fires on page load (confirmed via Google Tag Assistant)
- [ ] `raw_first_source` populated from UTM parameters on every `inbound_lead_received` event
- [ ] UTM parameters preserved through the GHL funnel redirect chain (no stripping)
- [ ] Test lead submitted via `?utm_source=test&utm_medium=email`; confirm `raw_first_source = 'test/email'` on the resulting `inbound_messages` row

---

### Rent-Estimate Landing Page

**Week:** 3 *(assumed)*

**Description**
A client-branded landing page where a property owner enters an address and receives a rent estimate with a booking link underneath it. Estimate sourced from the RentCast API. Page built on GHL funnels, not a custom build. Config-driven client branding.

**Notes**
Not a founding pilot launch blocker; client-facing marketing tool needed soon after pilot. 4–6 hours estimated.

**Definition of Done**
- [ ] Address input returns a rent estimate from RentCast API within 3 seconds
- [ ] Booking link appears below the estimate and routes to the client's GHL calendar
- [ ] Page is client-branded from config (logo, name, colours) — no hardcoded Blackink branding visible
- [ ] Built on GHL funnels — no custom server infrastructure
- [ ] RentCast API key stored in config, not hardcoded

---

### ZIP-Level Landing Pages

**Week:** 3 *(assumed)*

**Description**
Templated landing pages generated from the zone map — one page per ZIP code, client-branded, with schema markup for local SEO. Built on GHL funnels or a static generator, whichever is faster to deploy.

**Notes**
Marketing and SEO asset; no operational dependency for the founding pilot. 6–10 hours estimated. Choose the faster build path and document the choice.

**Definition of Done**
- [ ] One page generated per active ZIP in the client's zone map
- [ ] Each page is client-branded and contains correct ZIP-specific content
- [ ] Schema markup present and validates against Google's Rich Results Test
- [ ] Pages accessible via a predictable URL pattern
- [ ] Build method documented (GHL funnels or static generator) with rationale

---

### Ryse Eligibility Flag

**Week:** Post-September

> **Do not build in September.** Dependent on the Ryse partnership agreement and API credentials being finalised. No scope commitment until both are received.

**Description**
For properties actively under the client's management, surface a Ryse financing eligibility flag on the Owner Packet. Owner-facing copy states eligibility begins at signing. The client receives a 2% referral fee on every advance. Fires only for managed properties — not for prospecting targets.

**Notes**
Financial product integration (6–10 hrs). Not a September scope item.

**Definition of Done**
- [ ] Ryse partnership agreement confirmed and API credentials received
- [ ] `ryse_eligible` flag evaluated only for properties with an active management agreement
- [ ] Owner Packet displays eligibility notice with correct copy ("eligibility begins at signing")
- [ ] 2% referral fee tracked in the settlement ledger per advance
- [ ] Ryse eligibility does not appear on prospecting targets (owners not yet under management)

---

## 7. Summary Index

| # | Item | Week |
|---|---|---|
| 1 | Three Founding SKUs | 2 |
| 2 | `founding = true` Flag | 2 |
| 3 | No List Price Stored or Shown | 2 |
| 4 | Full County Exclusivity Lock | 2 |
| 5 | No Monthly Ceiling on Sits | 2 |
| 6 | Demo Kit Config Correction | 2 |
| 7 | $50 Miss Credit | 2 |
| 8 | First Sit Free | 2 |
| 9 | 60-Day Guarantee | 2 |
| 10 | Dispute Credited on Flagging | 2 |
| 11 | Skip-Trace Confirmation | 2 |
| 12 | Win-Back Assessor/FRBO Match | 2 |
| 13 | Six-Attempt Inbound Cadence | 2 |
| 14 | Pay-Per-Lead Routing | 2 |
| 15 | Live Inbound Trigger in Demo | 2 |
| 16 | Pixel + UTM Plumbing | 2 |
| 17 | Homestead Exemption Dropped Signal | 3 *(assumed)* |
| 18 | Whale Ranking + Out-of-State Tier | 3 *(assumed)* |
| 19 | Direct Mail Merge | 3 *(assumed)* |
| 20 | Post-Sit Nurture Sequence | 3 *(assumed)* |
| 21 | `referral_partner` Table | 3 *(assumed)* |
| 22 | `sell_intent` Disposition | 3 *(assumed)* |
| 23 | `signal_surfaced_to` Event | 3 *(assumed)* |
| 24 | ZIP-Match Routing for Partner Signals | 3 *(assumed)* |
| 25 | Monday Partner Digest | 3 *(assumed)* |
| 26 | 48-Hour Report-Back | 3 *(assumed)* |
| 27 | Partner Scoreboard | 3 *(assumed)* |
| 28 | Rent-Estimate Landing Page | 3 *(assumed)* |
| 29 | ZIP-Level Landing Pages | 3 *(assumed)* |
| 30 | Same-Owner Assessor Match | 4 *(assumed)* |
| 31 | HOA Rental-Cap Flag | 4 *(assumed)* |
| 32 | Review-Velocity Trigger | 4 *(assumed)* |
| 33 | Ryse Eligibility Flag | Post-September |
| 34 | Dead-Lead 9-Touch SMS Drip | ⛔ BLOCKED |
