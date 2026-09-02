# Blackink Week 0 — Dev Task Split

**Aug 31 – Sept 2, 2026 | 4 Developers | 10DLC filing excluded (handled separately)**

Reference: spec §1 (Data Contracts), §2 (Architectural Invariants), §3.0 (Week 0). Cross-referenced
against the client's Week 0 Q&A answers (`Blackink_Week_0_Week_1_Open_Items_with_Answers.md`).
This is a **fork of the Forced Action codebase** (reusing Cora, Vera, Hunter, Relay) with its own
dedicated repo, dev, and production servers — not a greenfield build. Scope estimates should be cut
against reuse, not against building from zero.

---

## Infrastructure — Provisioned

Two Hetzner servers, joined by a private network (vLAN):

| Server              | Spec                                            | Role                                                               |
| ------------------- | ----------------------------------------------- | ------------------------------------------------------------------ |
| `blackink-master` | CCX23 — 4 vCPU / 16GB / 160GB (dedicated vCPU) | App, API, Slack bot, Postgres, Redis, ghost-shopper/crawler worker |
| `blackink-hunter` | CPX21 — 3 vCPU / 4GB / 80GB (shared vCPU)      | Hunter entity-resolution worker only — no local database          |

**What to do:**

- Hunter has **no database of its own** — it connects to `blackink-master`'s Postgres over the
  private network, reads raw records, does matching/scoring locally, writes results back. One source
  of truth, not a synced copy.
- Firewall: SSH restricted to team IPs on both boxes; Postgres port (5432) closed on the public
  interface, reachable only over the private network from the Hunter box.
- Create a **dedicated, least-privilege Postgres user for Hunter's worker** — not the main app's
  credentials. Read access to source tables, write access only to resolved-entity/scoring tables.
- Automated nightly backup (`pg_dump` + WAL archiving to off-server object storage, not just a
  Hetzner snapshot) — **and one test restore actually performed before the first paying client.**
  This is a client-mandated gate, not optional.
- **No production PII in dev or staging environments.** If a dev/staging DB copy is spun up, scrub or
  fake contact data first.
- Provision production before the `events` outcome table is written to for real.

---

## Dev 1 — Data Infrastructure & Pipeline

**Owner:** Akrash handoff, multi-tenant schema, compliance gate SQL

### 1. Tenant keying (§3.0.1, pattern adoption)

**What to do:** Establish row-level `client_id` on every primary table, every view, and every Redis
cache key. Blackink's own self-marketing activity uses a dedicated internal `client_id`, same as any
other tenant — no special-cased "no tenant" path.

### 2. Entity normalization refactor (§3.0.1, refactored)

**What to do:** Adapt entity-resolution routines to distinguish a generic property contact from a
corporate LLC owner holding a multi-unit portfolio — this is the same distinction Hunter needs later,
build the normalization logic here so Hunter's worker consumes clean input.

### 3. `raw_prospect_pipeline` staging schema (§1, §3.0.5) — full field list

**What to do:** Build the complete schema, not just the headline fields. Per spec §1:

- **Company/entity:** `company_id` (deterministic **SHA-256 hash of normalized domain** — not a
  random UUID), `company_name`, `website`, `domain` (UNIQUE), `market_metro`, `door_count_est`,
  `current_pm_software`
- **Contacts (two per company):** `contact_id`, `contact_role_type` (`OWNER_BROKER_MD` /
  `OFFICE_MANAGER_OPS`), `first_name`, `last_name`, `title`, `email`, `email_status` (`VERIFIED` /
  `ESTIMATED` / `UNVERIFIED` / `BOUNCED`), `phone` (E.164), `phone_type` (`MOBILE` / `DIRECT_WORK` /
  `OFFICE_LANDLINE`), `linkedin_url`
- **Lineage:** `source_channel`, `source_timestamp`, `enrichment_timestamp`, `enrichment_provider`
- **Audit/personalization payload:** `audit_speed_score_sec`, `audit_loss_dollars_est`,
  `personalized_video_id`, `custom_hook_text` (nullable — populated later in Week 1, columns exist now)
- **Compliance:** `suppression_state`, `dnc_clean`, `is_opted_out`, `compliance_eligibility`
  (`EMAIL_COLD_ELIGIBLE` / `TRANSACTIONAL_SMS_ONLY` / `BLOCKED`)
- Cross-check against the client's Week 1 Q&A minimum schema (`firm_name`, `county`,
  `door_count_source`, `validation_status`, `reject_reason_code`) and reconcile field naming before
  Akrash starts writing — don't let the two schemas drift.

### 4. `evaluate_campaign_readiness()` gate function (§1)

**What to do:** Implement the exact predicate function from spec §1 in
`src/compliance/gate_evaluator.py`. It must check, in one query: email verified, not opted out, DNC
clean, not suppressed, `compliance_eligibility = EMAIL_COLD_ELIGIBLE`, **an audit payload exists**
(`audit_speed_score_sec IS NOT NULL OR personalized_video_id IS NOT NULL`), **no match in
`client_pm_books`** (non-poach), and **the 14-day re-touch cooling rule**
(`last_outbound_touch_at IS NULL OR <= NOW() - INTERVAL '14 days'`). A record only flips to
`READY_FOR_CAMPAIGN` when every predicate passes.

### 5. `client_pm_books` table (new — required by #4, not previously listed)

**What to do:** Create this table now. It's what the non-poach gate joins against
(`owner_domain`, `owner_email` columns minimum). Empty at Week 0 close is fine — Week 2's PM
software read-sync populates it — but the table and the join must exist before the gate can run.

### 6. Akrash staging handoff (§3.0.5)

**What to do:** Provision Akrash a restricted-write login — only `raw_prospect_pipeline`, nothing
else. Publish the schema + validation rules as a spec, don't wait for the first real batch to define it.

### 7. DNS & mailbox warmup (§3.0.5)

**What to do:** Get DNS access delegated; configure SPF, DKIM, DMARC across 20 domains (40
mailboxes); start warmup schedules now — warmup takes weeks and Week 1 dispatch depends on it.

### 8. Minimal `events` table (new — unblocks Dev 2)

**What to do:** Land a bare-bones version of the `events` table (spec §3.1.1's full schema is a Week 1
item, but Dev 2's Vera tri-state fix needs somewhere to write `UNKNOWN`/`ABSTAIN` states now). Ship
the minimal columns (`event_id`, `client_id`, `event_type`, `payload`, `occurred_at`) in Week 0; the
full migration lands in Week 1 without breaking this.

### Acceptance Criteria Owned

- **AC #6:** Signed data interface spec approved, `raw_prospect_pipeline` schema live in staging

---

## Dev 2 — Platform Defect Remediation

**Owner:** All four 3.0.2 fixes, scoped per the client's Q&A answers

### A. Vera Silent-Zero Remediation — scope narrowed by client answer

**What to do (three separate deliverables, don't blend them):**

1. **The actual fix (8-hour budget):** revenue and billing reconciliation only — when Stripe,
   calendar logs, or PM software read feeds are unreachable or return missing values, return
   `UNKNOWN` or `ABSTAIN`, halt downstream settlement, alert admins. Zero silent `0` returns in this path.
2. **Make `UNKNOWN`/`ABSTAIN` a first-class return type in the Shared Agent Core.** If only one
   thing from this item ships, it's this — every other agent that later needs tri-state logic builds
   on it.
3. **Separate 2-hour audit:** sweep the rest of the codebase for other silent-zero patterns, hand
   back a **severity-ranked list**. Do not silently fix anything found outside the 8-hour budget —
   report only.

### B. Relay Persistent Halt & TTL Re-Arm Patch

**What to do:** Update scheduler and queue worker state machines so global, client-level, and
campaign-level pause states persist in both Redis and PostgreSQL. Remove all TTL-based auto-resume
logic entirely. Queues stay locked until an authorized admin issues a cryptographic resume command
via Slack.
**Joint with Dev 3:** the backend persistence is Dev 2's; the Slack-side command that issues the
cryptographic resume is Dev 3's (§3.0.3's hash-verification work). Coordinate the payload format
before either side builds against it.

### C. Cora Queue Throttling — thresholds confirmed

**What to do:** Auto-pause new draft generation when unreviewed Slack approval queue hits **50
items**, OR when the oldest unreviewed draft exceeds **24 hours**, whichever fires first — two
independent triggers, not one. The pause must stop *generation*, not just sends. One Slack
notification on entering the paused state (not one per blocked attempt). Resume must be clean: no
duplicate or lost items when generation restarts as the queue clears.

### D. Hunter Entity Resolution Worker — infra decided, build the worker

**What to do:** `blackink-hunter` (CPX21) is provisioned. Build:

- The nightly async sweep job: resolve corporate names, registered agents, and individual property
  owners across fragmented multi-property LLC portfolios
- Connection to `blackink-master`'s Postgres over the private network using the dedicated
  restricted DB user (see Infrastructure section above) — no local database on this box
- No cross-table joins against the primary application DB from the main app process — this is the
  whole point of the separate box

### Acceptance Criteria Owned

- **AC #2:** Vera health jobs output `UNKNOWN`/`ABSTAIN` on missing inputs — zero silent-zero returns
  in the revenue/billing path
- **AC #3:** Emergency pause triggered via Slack persists across server restarts; only authorized
  resume clears it
- **AC #4:** Hunter standalone worker resolves a sample batch of property owner entities across
  fragmented LLCs

---

## Dev 3 — Slack Agent Hub (@Blackink)

**Owner:** Full @Blackink interactive cockpit

### 1. Port async execution harnesses (§3.0.1, direct fork)

**What to do:** Port queue runners, dispatchers, and state machines from the Forced Action codebase
as-is.

### 2. Port Slack interactive state machines (§3.0.1, direct fork)

**What to do:** Port Block Kit button handlers (`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`) and modal submission listeners.

### 3. Stand up @Blackink app + channels (§3.0.3, net new)

**What to do:** Configure the app as the central command/intent dispatcher, and build all six
operational channels:

- `#blackink-command` — executive overview, global pause/resume
- `#blackink-setter` — 1-screen context cards for high-intent leads
- `#sales-replies` — inbound reply stream, intent-tagged
- `#dial-tasks` — daily phone queue
- `#blackink-qa` — health logs, API heartbeat failures, domain reputation deltas, **cross-tenant
  leakage alerts** (see Dev 4's new CI test below — this channel is where its failures should post)
- `#blackink-economics` — CAC, unit economics, wallet caps

### 4. Payload-bound hash verification (§3.0.3)

**What to do:** Bind every interactive card to a SHA-256 hash of its exact message payload +
recipient identifier + config state. A click on a card whose underlying payload changed since it was
posted must be rejected by the backend.

### 5. Cryptographic resume command — joint with Dev 2

**What to do:** Build the Slack-side command/button that issues the resume signal Dev 2's backend
checks for (item B above). Agree on the exact payload/signature format with Dev 2 before either side
builds against it.

### Acceptance Criteria Owned

- **AC #1:** @Blackink app active in workspace, posting native action cards and processing button
  clicks with hash verification

---

## Dev 4 — Compliance Gates, Suppression, CI, & Reuse Ledger

**Owner:** DNC/suppression, outbound compliance, cross-tenant CI tests, final Reuse Ledger doc

### 1. Port drafting patterns & DNC scrubbing (§3.0.1, direct fork)

**What to do:** Port LLM prompt templates and structured output formatters. Port deterministic DNC
matching routines against state and national registries — no LLM in this path.

### 2. Refactor dispatch adapters & audit pipelines (§3.0.1, refactored)

**What to do:** Decouple pooled sending identities into strict per-tenant mailbox assignments — no
cross-tenant bleed. Modularize the PDF loss-report generator to consume property-management speed
metrics instead of the old general distress scoring.

### 3. Deterministic gate enforcement (§3.0.1, pattern adoption)

**What to do:** Hard-code pre-send policy checks — suppression, quiet hours, channel eligibility —
that fully bypass any LLM. This is the code Dev 1's `evaluate_campaign_readiness()` and this
suppression logic both feed into; keep them consistent.

### 4. CI/CD compliance gates

**What to do, two separate gates:**

- Cold-SMS gate: build fails if code attempts outbound SMS to any contact where
  `inbound_sms_count = 0 AND booked_appointment_id IS NULL`
- **Cross-tenant leakage tests (new — §2 invariant 5, previously unassigned):** automated CI tests
  that verify no query, cache key, or API response can return another `client_id`'s data. Run on
  every build, not just at release. Failures should be loud enough to hit `#blackink-qa`.

### 5. Quiet hours & template enforcement

**What to do:** No SMS delivery 9 PM–8 AM recipient local time. Every outbound template requires the
`{client_firm}` tag.

### 6. Reuse Ledger (§3.0.1 deliverable)

**What to do:** Compile and deliver at Week 0 close:

- Components ported (direct fork) — per-dev list, since all four devs port something
- Architectural refactors completed
- Pattern-only adoptions
- **Automated test pass rate per module** — pull this from each dev, don't estimate it yourself

### Acceptance Criteria Owned

- **AC #5:** CI/CD tests blocking cold outbound SMS are live and passing (plus cross-tenant leakage
  tests, now folded into this AC)
- **AC #7:** Complete module-by-module Reuse Ledger delivered with test coverage documented

---

## Parked — Not Dev-Assigned

| Item                                            | Owner                | Notes                                                                                                       |
| ----------------------------------------------- | -------------------- | ----------------------------------------------------------------------------------------------------------- |
| A2P 10DLC Brand + Campaign Registration (3.0.4) | Non-dev / compliance | Filed via Telnyx/TCR portal; legal entity HEU AI LLC; mixed use case (Customer Care + Account Notification) |

---

## Acceptance Criteria Summary

| AC                                                          | Owner                 |
| ----------------------------------------------------------- | --------------------- |
| #1 — Live Slack cockpit with hash verification             | Dev 3                 |
| #2 — Vera zero silent-zero returns (revenue/billing scope) | Dev 2                 |
| #3 — Persistent halt survives restarts                     | Dev 2 + Dev 3 (joint) |
| #4 — Hunter entity resolution batch passes                 | Dev 2                 |
| #5 — CI/CD SMS gate + cross-tenant leakage tests live      | Dev 4                 |
| #6 — Signed data contract + staging schema live            | Dev 1                 |
| #7 — Reuse Ledger delivered                                | Dev 4                 |
| A2P 10DLC submitted                                         | Parked                |

## Hard dependencies across devs

- **Dev 1's minimal `events` table and `client_pm_books` table block Dev 2 and Dev 4** — land these
  early in the week, not at the end.
- **Dev 1's `evaluate_campaign_readiness()` and Dev 4's deterministic gate logic must agree on the
  same predicates** — don't build these independently and reconcile later.
- **Dev 2 and Dev 3 must agree on the resume-command payload format** before either builds their half.
- **Infra (main + Hunter servers, private network) is done** — Dev 2 can start on Hunter's worker
  code immediately rather than waiting on provisioning.
