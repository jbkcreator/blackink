# Blackink Week 1 — Dev Task Split
**Sept 1 – Sept 11, 2026 | 4 Developers | Milestone: MARKETING LIVE (Sept 11)**

Source: spec §3.1.1 – §3.1.8. Go-live is a **live demonstration contract** (§3.1.8) — six things must be
demonstrated end-to-end on real Florida prospect data, not just merged.

---

## Dev 1 — Data Spine, Ingestion & Compliance Gate

**Owner:** §3.1.1, §3.1.2

### Core spine migration (`src/db/migrations/001_core_spine.sql`)
- `events` — the shared ledger of record. `event_id`, `client_id` (NOT NULL, tenant scope; Blackink
  self-marketing uses a dedicated internal `client_id`), `owner_id`, `property_id`, `campaign_id`,
  `event_type`, `source`, `value_cents`, `payload` JSONB, `occurred_at`
- `companies` — apex `domain` UNIQUE, `market_metro`, `door_count_est`, `current_pm_software`,
  `status` (`PROSPECTING` → `ENGAGED` → `DEMO_BOOKED` → `CLIENT` / `EXCLUDED`)
- `contacts` — two-contact model, all compliance flags (`is_opted_out`, `dnc_clean`,
  `suppression_state`, `compliance_eligibility`, `last_outbound_touch_at`)
- `pm_profiles` — future-proofing metadata (specialty tags, languages, asset-class strengths,
  coverage polygon, historical close rate, avg speed-to-lead, show rate)
- Indices: `idx_events_client_type`, `idx_contacts_lookup`, `idx_companies_domain`

### Ingestion pipeline (staging → primary)
- Promote `raw_prospect_pipeline` records into `companies` + `contacts`; validate apex-domain
  uniqueness, normalize names/phones (E.164)
- Two-contact resolution per firm: **Contact A `OWNER_BROKER_MD`** (broker/owner/president/CEO —
  gets financial-proof messaging) and **Contact B `OFFICE_MANAGER_OPS`** (ops/leasing lead — gets
  velocity-proof messaging). Role tag drives template selection downstream.
- Target volume: **500–1,000 PM firms** across Tampa/St. Pete, Orlando, Miami-Dade

### Deterministic compliance gate (§3.1.2) — no LLM in this path, ever
Four ordered stages, each with an explicit terminal state:
1. Global / explicit opt-out → `PERMANENTLY_BLOCKED`
2. Cross-client non-poach match (`client_pm_books`) → `SUPPRESSED_AND_LOGGED`
3. DNC registry + quiet hours → `CHANNEL_SUPPRESSED`
4. Warm-channel waterfall routing → cold tier vs. engaged tier
- **Cold tier:** email eligible (verified only), human phone → `#dial-tasks`, **cold SMS banned**
- **Engaged tier:** email + transactional SMS unlocked (10DLC customer-care)
- SMS unlock predicate: inbound message received OR appointment confirmed on calendar. Enforce at
  DB, application, and CI layers.
- **Non-poach sync:** read-only connections into active clients' PM software pull current owner
  rosters into the central suppression table; gate checks target domain, owner name, and parcel
- **Metro allocation algorithm:** when multiple client firms share a metro, an owner is allocated to
  one campaign at a time by portfolio size + operational fit; 30-day inaction timer re-evaluates
- **DNC scrubbing:** pre-send linter hits live national + FL registries, strips dial/SMS eligibility

### Owns from §3.1.8
- **DoD #6** — CI/CD compliance suite proves cold SMS is hard-blocked at gate and runtime linter

---

## Dev 2 — Outbound Proof Machine (Audit Factory)

**Owner:** §3.1.3

### A. Ghost-Shopper Agent
- Headless crawler finds the target firm's owner-inquiry / contact form and submits a standardized
  professional owner inquiry
- Record submission time to the **millisecond**
- Stand up inbound webhook + IMAP listener; on reply, capture timestamp, compute
  `audit_speed_score_sec`, log raw interaction to `events`
- No reply in 24h → flag `UNRESPONSIVE_OVER_24H`

### B. Dynamic PDF Loss Report Compiler
- Model:
  - `Lost Inquiries = Monthly Leads × (1 − e^(−0.0005 × latency_seconds))`
  - `Annual Lost Revenue = Lost Inquiries × (Avg Monthly Mgmt Fee × 12) × Avg Door Retention (yrs)`
- Branded 2-page executive PDF containing: (1) timestamped audit log — their submission vs. first
  response; (2) metro comparison ("you responded in 4h12m; Tampa top-10% respond under 9 minutes");
  (3) estimated annual management + leasing revenue lost to faster competitors
- Write `audit_loss_dollars_est` back to the record

### C. Sendspark dynamic video merge
- REST integration — **no on-server video rendering**
- Landing page URL pattern:
  `https://watch.blackink.io/v/{company_id}?company={company_name}&speed={audit_speed_score_sec}&loss={audit_loss_dollars_est}`
- Animated GIF thumbnail: prospect's own website with their speed score overlaid
- Consume Sendspark webhooks `video_watched_50_percent`, `video_completed` → log to `events`,
  notify reps in real time

### D. Fee-Stack One-Pager Generator (ADD-8-Lite)
- Reuse the templated merge pipeline from (B)
- Maps fee leakage: uncollected lease-renewal fees, maintenance markups, tenant setup fees, pet rent
  share, resident benefits packages
- Visualizes revenue lift from activating ancillary partner programs (Ryse rent advances, utility
  concierge)

### Owns from §3.1.8
- **DoD #2** — live inquiry submitted through a real PM site → latency captured → PDF generated →
  Sendspark merge params verified clean

---

## Dev 3 — Sequencer, Reply Bridge & Booking Engine

**Owner:** §3.1.4, §3.1.5

### Multi-touch sequencer (§3.1.4)
| Touch | Channel | Day | Content | Governance rule |
|---|---|---|---|---|
| 1 | Cold email | 0 | Speed audit PDF + Sendspark video + micro-ask "Reply YES to see where you rank in Tampa" | Verified email only; pixel active |
| 2 | Human phone task | 1–2 | Audit follow-up, references video-view data | Verified direct line; local calling hours |
| 3 | Cold email | 4 | Fee-stack / ADD-8-Lite + Ryse angle | Threaded to Email 1; re-check opt-out and no prior reply |
| 4 | LinkedIn deep-link (manual) | 7 | Profile URL + tailored note copied to setter clipboard | Logged as manual task; **zero headless browser scraping** |
| 5 | Cold email | 10 | Metro Speed Index + territory-lock scarcity | Final cold touch, then 30-day cooling |
| Cond. | SMS nudge | post-engage | Direct scheduling nudge | Blocked unless an explicit engagement event exists in DB |

- Dispatch from warmed Google Workspace / Outlook mailboxes across the dedicated domains built in
  Week 0 (20 domains / 40 mailboxes), SPF+DKIM+DMARC verified
- Every dispatch calls Dev 1's gate first; every dispatch writes an
  `outbound_touch_dispatched` event with the full payload shape in §3.1.7

### Interim Reply Bridge (§3.1.4) — live Sept 11–16 only
Bridges the gap until the Week 2 Reply Triage Agent ships.
- Inbound reply webhook receiver (email + SMS), instant
- `#sales-replies` card: prospect name, company domain, door count, full thread history, buttons
  `Reply in Thread`, `Book Meeting`, `Mark Opt-Out`
- `#dial-tasks` bridge: direct dial number, recipient local timezone, Sendspark watch percentage

### Booking engine & show-rate cascade (§3.1.5)
- Calendly / Google Calendar integration; webhook captures name, work email, mobile, door count →
  creates `meeting_booked` event
- On booking: branded confirmation email + ICS, and transactional A2P SMS confirmation
- T-24h: reminder email with prep context + link to seeded demo video
- Morning-of 8:00 AM **recipient local time**: short SMS ping + one-tap reschedule/cancel
- **T-30min: pre-demo lead-in email** — attaches their custom audit PDF, gives the Rent Bot number,
  prompts "text any property address to test live"
- **No-show handler:** no attendance within 10 min of start → rep hits `Mark No-Show` in Slack →
  pauses sequences, enqueues multi-channel re-booking recovery flow

### Self-serve audit landing page (3.6-Pixel)
- Public page: PM enters corporate domain → triggers background ghost-shopper worker → captures lead
  → redirects high-intent to the booking calendar
- Meta + Google pixels for retargeting audiences, under strict wallet budget caps

### Owns from §3.1.8
- **DoD #1** — live automated multi-touch dispatch from warmed inboxes
- **DoD #3** — live booking → `meeting_booked` row in Postgres → email + SMS confirmations land
- **DoD #5** — approve / revise / snooze work in `#blackink-setter` and `#sales-replies`, and a
  tampered payload is rejected by the Week 0 hash check

---

## Dev 4 — Demo Weapons, Rent Bot & Metrics

**Owner:** §3.1.6, §3.1.7

### Rent Analysis Bot — minimum real version (BOT-MIN)
- Dedicated Twilio number, SMS webhook at `/api/v1/rentbot/demo`
- Address parsing + normalization engine
- Live path: CoreLogic / RentCast valuation call (~15–30s)
- **Demo-mode fallback cache** (non-negotiable — this runs during live pitches): on API timeout or
  failure, serve a pre-computed metro valuation in **under 5 seconds**
- Reply format, dispatched in **under 60 seconds**:
  `"123 Ocean Dr, Tampa: Est Rent $2,450/mo (Range $2.3k–$2.6k) Confidence Score: 94%. Powered by Blackink Rent Engine."`

### Permanent Demo Friday sandbox client
- Formally designated permanent test tenant, populated with realistic operational data
- On-demand during sales calls: live Looker dashboards, active mock campaigns, lead-matching logs,
  sample performance evidence packets

### 60-second post-meeting form
- Slack modal fired after every completed meeting, capturing: attendance (`Held` / `No-Show` /
  `Rescheduled`), target PM software, estimated door count, stated objections (`Pricing` /
  `Software Integration` / `Capacity` / `Existing Agency`), next action
- Feeds the Owner Score ranking engine and the Prospecting Agent

### Pipeline reporting & Slack digest (1.6-Pipe)
- Looker Studio dashboards on **Postgres read-replicas** (never the primary)
- Daily morning digest to `#blackink-command`: audits completed, avg metro response latency, cold
  emails dispatched, open / CTR / video-completion rates, appointments booked

### Owns from §3.1.8
- **DoD #4** — pre-demo lead-in fires at T-30min; a live Florida address texted to the Rent Bot
  returns an accurate valuation in under 60 seconds

---

## Week 1 Definition of Done (§3.1.8) — the Sept 11 demo contract

| # | Demonstration | Owner |
|---|---|---|
| 1 | Live multi-touch email dispatch from warmed domains, SPF/DKIM/DMARC verified | Dev 3 |
| 2 | End-to-end ghost-shopper: real site → latency → PDF → Sendspark merge | Dev 2 |
| 3 | Live booking → `meeting_booked` event → email + transactional SMS confirmations | Dev 3 |
| 4 | T-30min lead-in email + Rent Bot SMS valuation under 60s | Dev 4 |
| 5 | Slack approve/revise/snooze with payload-hash tamper rejection | Dev 3 |
| 6 | CI/CD suite proves zero cold SMS to unconsented contacts | Dev 1 |

## Hard sequencing dependencies

- **Dev 1's core spine blocks everyone.** Land `001_core_spine.sql` first; every other stream
  writes to `events`.
- **Dev 2's ghost-shopper blocks Dev 3's Touch 1** — no audit score means no PDF, no video, no
  Email 1 content.
- **Dev 3's sequencer cannot dispatch until Dev 1's gate is callable.** Wire the gate call before
  the first send, not after.
- **Dev 4's Rent Bot needs the Twilio number provisioned under the Week 0 10DLC brand.** If the
  10DLC filing is still pending, the bot demos on the fallback cache path.
- **Week 0 overlaps Week 1** (Week 0 ends Sept 2, Week 1 starts Sept 1). Week 0 remediation work and
  Week 1 build run concurrently for two days.
