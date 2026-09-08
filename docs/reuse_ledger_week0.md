# Week 0 Reuse Ledger — Full (All 4 Devs)

**Sprint:** Week 0 (Aug 31 – Sept 2, 2026)  
**Branch:** `dev4/compliance-gates-suppression` (Dev 4 commits); Dev 1–3 branches pending merge  
**Compiled by:** Dev 4 from codebase + sprint file  
**Gate 1 sign-off deliverable** — AC #7

---

## Table of Contents

1. [Dev 1 — Data Infrastructure & Pipeline](#dev-1--data-infrastructure--pipeline)
2. [Dev 2 — Platform Defect Remediation](#dev-2--platform-defect-remediation)
3. [Dev 3 — Slack Agent Hub](#dev-3--slack-agent-hub-blackink)
4. [Dev 4 — Compliance Gates, Suppression & Dispatch](#dev-4--compliance-gates-suppression--dispatch)
5. [FA Modules Explicitly NOT Ported (Week 0)](#fa-modules-explicitly-not-ported-week-0)
6. [Acceptance Criteria Coverage](#acceptance-criteria-coverage)

---

## Dev 1 — Data Infrastructure & Pipeline

**Owner:** Data infra, Akrash handoff, multi-tenant schema  
**AC owned:** AC #6 (signed data contract + `raw_prospect_pipeline` schema live)

---

### 1.1 Three-Role Postgres Auth Model
**File:** `migrations/apply_db_roles.py`  
**Type:** Pattern adoption (FA → Blackink)  
**FA source:** `migrations/apply_vera_readonly_role.py` — role-creation structure  
**Key changes:**
- FA has 1 readonly role (`vera_readonly`) → Blackink has 3 purpose-built roles:
  - `blackink_app` — RLS-subject runtime (no BYPASSRLS)
  - `blackink_system` — BYPASSRLS, batch jobs only, grep-lint asserts never imported from `src/api/`
  - `akrash_ingest` — INSERT-only on staging tables, no other access
- Refuses to run if any password env var is unset (fail-loud, not silent)
- `--dry-run` flag for safe preview

**Tests:** Part of `tests/test_schema.py` schema validation suite  
**AC coverage:** Foundation for AC #6

---

### 1.2 Multi-Tenant RLS Chassis
**File:** `migrations/apply_rls_policies.py` + `config/tenant_policies.py`  
**Type:** New (FA had no RLS — identified as a root cause in FA ADR 0006)  
**FA source:** None — FA's `venture_key` was an unindexed, unenforced column with zero leakage tests  
**What it does:**
- `ENABLE ROW SECURITY` + `FORCE ROW SECURITY` on every table registered in `TENANT_POLICIES`
- Policy: `app.current_client_id` session variable (set by `session_scope(client_id=...)`) enforced at DB level
- Verification step at end of migration: **fails loudly** if any registered table lacks `rowsecurity=true AND forcerowsecurity=true`
- New tenant-bearing table not added to `TENANT_POLICIES` is caught here, not in production

**Tests:** `tests/test_tenant_isolation.py` — adversarial cross-tenant leakage tests (requires live DB)  
**AC coverage:** Architecture invariant underlying all ACs

---

### 1.3 `raw_prospect_pipeline` Staging Tables
**File:** `migrations/apply_raw_prospect_pipeline.py`  
**Type:** New (no FA equivalent — FA never had a multi-tenant ingestion pipeline with external handoff)  
**FA source:** None  
**What it does:**
- Two tables: `raw_prospect_companies` + `raw_prospect_contacts` (separate so each contact has independent `validation_status`)
- No `client_id` on either — Akrash has no tenant visibility; ownership assigned at promotion time via `county_allocations`
- `validation_status` vocabulary: `pending / cleared / quarantined / rejected`; every row that doesn't promote carries `reject_reason_code`
- Required fields per Akrash data interface: `company_id` (SHA-256 of domain), `company_name`, `domain`, `market_metro`, `door_count_est`, two contacts with verified email + direct phone
- Akrash write access via `akrash_ingest` role (INSERT-only), granted separately in `apply_akrash_grant.py`

**Tests:** Schema validation in `tests/test_schema.py`  
**AC coverage:** AC #6 (staging schema live)

---

### 1.4 Quarantine Gate
**File:** `src/services/quarantine_gate.py`  
**Type:** New (pattern-adopted from compliance_gate.py's pluggable-provider shape)  
**FA source:** None  
**What it does:**
- Governs promotion eligibility (raw → canonical tables), distinct from `compliance_gate.py` (send eligibility)
- Reject-reason vocabulary: `EMAIL_INVALID_SYNTAX`, `EMAIL_HARD_BOUNCE`, `EMAIL_VERIFY_TIMEOUT` (quarantine/retryable), `DNC_LISTED`, `DNC_VERIFY_TIMEOUT`, `GLOBAL_OPT_OUT`, `NONPOACH_CONFLICT`, `MISSING_REQUIRED_FIELD:<field>`, `DUPLICATE_COMPANY`
- Shares `DncProvider` / `EmailVerificationProvider` interfaces with `compliance_gate.py` — no duplicate vendor code
- Nothing dropped silently: every non-cleared row has exactly one `reason_code`

**Tests:** Part of `tests/test_compliance_gate.py` suite  
**AC coverage:** AC #6 (quarantine gate ensures only clean data promotes)

---

### 1.5 `company_id` Deterministic Identity
**File:** `src/core/models.py` + `src/loaders/base.py`  
**Type:** New (FA used `gen_random_uuid()` — identified as defeating global dedup)  
**FA source:** None — FA's company IDs were random UUIDs  
**What it does:** `company_id = SHA-256(normalized_domain)`. Same company submitted by different sources always resolves to the same ID. Enforced in `BaseIngestLoader.compute_company_id()`.

**Tests:** Schema tests  
**AC coverage:** Foundation for global dedup, AC #6

---

### 1.6 Promotion Sweep Task
**File:** `src/tasks/promotion_sweep.py`  
**Type:** New  
**FA source:** None  
**What it does:** Runs every few minutes as `blackink_system` (BYPASSRLS). Validates, dedupes, promotes `cleared` raw rows into `companies`/`contacts`. A company can promote with one clean contact. Assigns `owning_client_id` via `county_allocations` join.

**Tests:** Input pending from Dev 1  
**AC coverage:** AC #6

---

### 1.7 DNS / Deliverability Setup (20 domains / 40 mailboxes)
**Type:** Manual runbook (not code) — per CLAUDE.md and FA ADR 0011  
**FA source:** FA ADR 0011 made the same call (DNS is manual)  
**What it does:** SPF, DKIM, DMARC configured across 20 domains (40 mailboxes); warmup schedules initialized. State tracked in `sending_domains` / `mailboxes` tables. `deliverability_sentinel.py` monitors bounce/complaint rates.

**Tests:** N/A (manual runbook)  
**AC coverage:** Pre-condition for Week 1 live outbound

---

## Dev 2 — Platform Defect Remediation

**Owner:** 4 platform bug fixes ported/refactored from FA  
**Branch:** `foundation/agent-relay-cora-hunter-vera`  
**AC owned:** AC #2 (Vera silent-zero), AC #3 (persistent halt), AC #4 (Hunter entity resolution)

---

### 2.1 Vera Silent-Zero Remediation
**Files:**
- `src/agents/vera/health_result.py` — tri-state type (`VALUE / UNKNOWN / ABSTAIN`)
- `src/agents/vera/checks/pm_feed.py` — PM software feed health check
- `src/agents/vera/checks/campaign_feed.py` — Instantly campaign feed check
- `src/agents/vera/checks/pipeline.py` — raw_prospect_pipeline health check
- `src/agents/vera/runner.py` — aggregates all three checks

**Type:** Refactor  
**FA source:** `src/services/vera_slack.py` + Vera data-reconciliation layer  
**What the code actually does:**
- `HealthResult` dataclass enforces strict tri-state: `VALUE` (real reading), `UNKNOWN` (source not configured — intentional skip), `ABSTAIN` (source configured but call failed)
- Silent-zero invariant documented in code: a check function MUST NOT return `VALUE` when data came from swallowing an exception
- `runner.run_health_checks()` returns a list — never collapses to a single boolean (collapsing was the silent-zero bug in aggregate form)
- DB-backed checks (pm_feed, pipeline) share one `system_session_scope()`; campaign_feed calls Instantly directly
- If DB session fails, both DB checks emit `ABSTAIN` — no VALUE returned on error

**Tests:** `tests/test_vera_health.py` — **23 tests**  
**AC coverage:** AC #2

---

### 2.2 Relay Persistent Halt & HMAC Resume
**Files:**
- `src/agents/relay/halt_state.py` — `HaltRecord` dataclass, scope constants (`GLOBAL / CLIENT / CAMPAIGN`)
- `src/agents/relay/halt_service.py` — `is_halted()`, `issue_halt()`, `resume_halt()`, `get_active_halts()`
- `src/agents/relay/resume_auth.py` — HMAC-SHA256 stateless token generation + verification
- `src/agents/relay/sync.py` — `sync_halts_from_db()`: Redis restore from Postgres on startup
- `migrations/apply_relay_halts.py` — `relay_halts` table (idempotent, partial unique index per active scope)

**Type:** Refactor  
**FA source:** `src/services/relay/` — TTL-based Redis-only pause state  
**What the code actually does:**
- Halt state stored in **both** Postgres (`relay_halts` table) and Redis (no TTL). Survives Redis flush or server restart
- `is_halted()` check order: (1) Redis fast path, (2) Postgres fallback if `relay:synced` flag absent (pre-sync window), (3) fail-open (False) if both layers unavailable — avoids deadlocking all workers during infra degradation
- Scope cascade: `GLOBAL` blocks all; `CLIENT` blocks all campaigns for that client; `CAMPAIGN` blocks one campaign
- `resume_halt()` requires a valid HMAC-SHA256 token generated at issue time — stateless, no stored token, deterministic given `RELAY_RESUME_SECRET + halt_id`
- Zero TTL-based auto-resume — lock holds until explicit authorized resume command
- `relay_halts` table is NOT in `TENANT_POLICIES` (control-plane state, not tenant-bearing)

**Tests:** `tests/test_relay_halt.py` — **30 tests** (fakeredis + FakeSession, no live DB/Redis)  
**AC coverage:** AC #3

---

### 2.3 Cora Approval Queue Throttle
**Files:**
- `src/agents/cora/throttle.py` — Redis-backed counter + hysteresis auto-pause flag
- `src/agents/cora/queue.py` — draft queuing with throttle gate
- `src/agents/cora/worker.py` — draft-generation worker loop
- `src/agents/cora/kill_switch.py` — hard kill switch (independent of throttle)

**Type:** Refactor  
**FA source:** `src/services/cora_throughput/` — draft orchestrator (no queue bounds)  
**What the code actually does:**
- Two Redis keys: `cora:approval:pending` (counter) and `cora:auto_paused` (presence flag)
- `DRAFT_QUEUE_CAPACITY = 50`, `RESUME_THRESHOLD = 40` — hysteresis prevents cycling at boundary
- `notify_draft_queued()` increments counter; `notify_approval_resolved()` (called by Dev 3 Slack handler) decrements it
- `is_auto_paused()` fails open (False) on Redis error — avoids deadlock; underlying halt still durable
- Keys are global (not tenant-scoped) for Week 0; comment in code notes per-client scoping as future path
- Dev 3 Slack bot calls `notify_approval_resolved()` when operator clicks Approve/Reject — cross-team interface point

**Tests:** `tests/test_cora_throttle.py` — **21 tests**  
**AC coverage:** System reliability (burst protection)

---

### 2.4 Hunter LLC Entity Resolution Worker
**Files:**
- `src/agents/hunter/entity_resolution.py` — fuzzy name clustering (rapidfuzz Union-Find, `SIMILARITY_THRESHOLD=85`)
- `src/agents/hunter/worker.py` — standalone nightly sweep worker
- `src/agents/hunter/registered_agent_provider.py` — registered agent lookup abstraction
- `src/agents/hunter/kill_switch.py` — hard kill switch
- `src/tasks/hunter_nightly_sweep.py` — scheduled task entry point

**Type:** Refactor (extracted from inline main-thread execution)  
**FA source:** `src/services/buyer_entity_resolution.py` — rapidfuzz entity resolution pattern  
**What the code actually does:**
- Algorithm: normalize company names (strip legal suffixes), Union-Find over all pairwise `WRatio >= 85` scores (transitive closure), each connected component → one `ResolvedCluster` with canonical name (shortest string in cluster — deterministic)
- Singletons (no match) → single-member cluster, `match_method='singleton'`
- Pure in-memory: no DB access in `entity_resolution.py` — worker handles reads/writes separately, so the core algorithm is testable without Postgres
- Standalone nightly sweep (`blackink_system` role) — not inline on API thread

**Tests:** `tests/test_hunter_entity_resolution.py` — **28 tests**  
**AC coverage:** AC #4

---

## Dev 3 — Slack Agent Hub (@Blackink)

**Owner:** @Blackink Slack app, 6 channels, SHA-256 payload binding  
**AC owned:** AC #1 (live Slack cockpit with hash verification)

> **Note:** Dev 3 is a direct port + net-new. Exact files and test counts are input pending from Dev 3. FA source mapping documented below.

---

### 3.1 Async Queue Runners & Dispatchers
**File:** *Input pending from Dev 3*  
**Type:** Direct port  
**FA source:** `src/services/action_queue.py` — async queue pattern, `ActionItem` shape, lane routing (`approvals` / `failures`)  
**Key changes:** Adapted for Blackink's `client_id`-scoped tenant context; FA had no multi-tenant isolation on queue items

**Tests:** Input pending from Dev 3  
**AC coverage:** AC #1 (underlying dispatch infrastructure)

---

### 3.2 Slack Block Kit Button Handlers
**File:** *Input pending from Dev 3*  
**Type:** Direct port  
**FA source:** `src/services/lifecycle_slack.py` — Block Kit patterns, `_KIND_PRELUDE`, WebClient usage; `src/services/vera_slack.py` — report posting pattern  
**Key changes:**
- FA handlers: `Approve / Revise / Reject / Snooze / Skip / Mark Done` → ported as-is
- **Added:** SHA-256 payload-bound hash verification on every interactive card. Each card bound to `hash(message_payload + recipient_id + config_state)`. Clicks on stale/modified cards rejected.
- FA had no hash verification — button clicks could be replayed or spoofed

**Tests:** Input pending from Dev 3  
**AC coverage:** AC #1

---

### 3.3 @Blackink Slack App + 6 Channels (Net New)
**File:** *Input pending from Dev 3*  
**Type:** New (no FA equivalent — FA had per-agent Slack integrations, not a centralized hub)  
**FA source:** None for hub architecture; `lifecycle_slack.py` / `vera_slack.py` for WebClient patterns  
**Channels provisioned:**

| Channel | Purpose |
|---|---|
| `#blackink-command` | Executive overview, macro pipeline queries, global pause/resume |
| `#blackink-setter` | 1-screen context cards for high-intent owner leads |
| `#sales-replies` | Inbound reply stream with automated intent tags |
| `#dial-tasks` | Prioritized daily phone queue (company background, response latency, direct line) |
| `#blackink-qa` | Health logs, API heartbeat failures, domain reputation deltas, cross-tenant leakage alerts |
| `#blackink-economics` | CAC rollups, channel unit economics, wallet cap dashboards |

**Tests:** Input pending from Dev 3  
**AC coverage:** AC #1

---

## Dev 4 — Compliance Gates, Suppression & Dispatch

**Owner:** DNC/suppression, outbound compliance, dispatch adapters, Reuse Ledger  
**Branch:** `dev4/compliance-gates-suppression`  
**Total tests:** 100 (all passing, no live DB required)  
**AC owned:** AC #5, AC #7

---

### 4.1 `src/services/outbound_templates.py`
**Type:** Pattern adoption (FA port + refactor)  
**FA source:** `src/services/email_templates.py` — `validate_variables()`, `build_instantly_sequence()`, `resolve_contact_variables()`  
**Key changes:**
- `{{double_brace}}` FA merge syntax → `{single_brace}` Instantly syntax
- `REQUIRED_TAGS = frozenset({"client_firm"})` — compliance enforcement at save-time AND dispatch-time
- `require_client_firm_tag()` hard gate
- `resolve_tags()` leaves unknown tags as-is (FA silently dropped them)
- `ALLOWED_TAGS` registry: 9 keys mapped to Blackink's PM schema

**Tests:** `tests/test_outbound_templates.py` — 12 tests  
**AC coverage:** AC #5 (template compliance), AC #7

---

### 4.2 `src/services/sms_quiet_hours.py`
**Type:** Pattern adoption (FA port)  
**FA source:** `src/services/sms_compliance.py` — `is_quiet_hours()`, `_AREA_CODE_TZ`  
**Key changes:**
- Replaced FA's national area-code map with Florida-only map (16 FL area codes)
- 850 (Panhandle) → `America/Chicago` (conservative over-suppress for ET/CST boundary)
- `assert_not_quiet_hours()` extracted as explicit gate function (FA bundled gate + send in one fn)
- `_recipient_tz()` extracted as testable unit

**Tests:** `tests/test_sms_quiet_hours.py` — 12 tests  
**AC coverage:** AC #5, AC #7

---

### 4.3 `src/services/cold_sms_gate.py`
**Type:** New (no FA equivalent — Blackink A2P 10DLC requirement)  
**FA source:** None  
**What it does:** CI/CD hard gate — blocks outbound SMS to contacts where `inbound_sms_count = 0 AND booked_appointment_id IS NULL`. Both signals derived from events ledger (no denormalized columns needed). Test suite IS the CI enforcement: if `assert_not_cold_sms` is removed, `test_gate_blocks_cold_contact` fails and build breaks.

**Tests:** `tests/test_cold_sms_gate.py` — 13 tests  
**AC coverage:** AC #5 (primary owner)

---

### 4.4 `src/services/email_suppression.py`
**Type:** Extension (Blackink stub → full port of FA cross-channel cascade shape)  
**FA source:** `src/services/email_suppression.py` — cross-channel cascade pattern  
**Key changes:**
- FA: separate `email_opt_outs` / `sms_opt_outs` tables + sibling-identifier lookup → Blackink: single `contacts` table (suppression is all-channel by definition)
- Added: `suppress_by_email()`, `suppress_by_phone()`, `suppress_by_domain()`, `bulk_suppress()`, `import_dnc_list()`
- Added: events ledger audit trail on every suppression write (`_log_suppression_event()`)
- `suppress_contact()` now takes `reason` string and logs it

**Tests:** `tests/test_email_suppression.py` — 20 tests  
**AC coverage:** AC #5, AC #7

---

### 4.5 `src/services/mailbox_dispatcher.py` + `migrations/apply_mailbox_last_used.py`
**Type:** New (no FA equivalent — FA is single-tenant)  
**FA source:** None  
**What it does:** Per-tenant round-robin mailbox picker. `SELECT FOR UPDATE SKIP LOCKED` prevents concurrent workers double-picking. Hard cross-tenant breach guard raises `RuntimeError` if DB returns wrong `client_id`. Raises `NoMailboxAvailable` — never falls back to another tenant's pool.  
**Migration:** Adds `last_used_at TIMESTAMPTZ` + partial index on `(client_id, last_used_at)` for `warmed+active` rows.

**Tests:** `tests/test_mailbox_dispatcher.py` — 12 tests  
**AC coverage:** AC #7

---

### 4.6 `src/services/audit_report.py`
**Type:** Refactor (FA LLM-based → deterministic)  
**FA source:** `src/services/loss_autopsy.py` — context-gathering structure  
**Key changes:**
- FA: Claude LLM classifies loss reasons from `distress_scores`, `deal_outcomes`, `closer_calls` → Blackink: deterministic (no LLM for compliance — blueprint invariant)
- Reads `events` payload (`audit_speed_score_sec`, `audit_loss_dollars_est`) written by Ghost-Shopper crawler
- Composable section builders (`section_speed_summary`, `section_loss_estimate`) for Week 2 Evidence Packet PDF
- `build_audit_context()` → merge tag dict for `outbound_templates.resolve_tags()`
- `assert_audit_complete()` gate blocks outbound if no audit data

**Tests:** `tests/test_audit_report.py` — 21 tests  
**AC coverage:** AC #7

---

### 4.7 `config/prompt_variants.py`
**Type:** Pattern adoption + rewrite (FA port)  
**FA source:** `config/prompt_variants.py` — champion-challenger registry pattern  
**Key changes:**
- FA: 1 email type (distressed-property investor) → Blackink: 3 email touches (Touch 1: Speed Loss Audit, Touch 3: Fee-Stack Opportunity, Touch 5: Metro Speed Index)
- Prompts rewritten for PM owner/broker audience + Blackink merge tag set (`{client_firm}`, `{audit_speed}`, `{loss_dollars}`, `{video_url}`, `{city}`)
- `{client_firm}` required in all champion prompts (enforced by test)
- `get_champion_prompt(touch)` raises `KeyError` for manual touches (2=phone, 4=LinkedIn)
- Challenger pool filtered by `golden_set_approved` (same guard as FA)

**Tests:** `tests/test_prompt_variants.py` — 11 tests  
**AC coverage:** AC #7

---

## FA Modules Explicitly NOT Ported (Week 0)

| FA module | Reason deferred |
|---|---|
| `src/services/prompt_experiment_engine.py` | Requires `AgentLaneExperiment` + experiment DB tables — Sprint 2 infrastructure |
| `src/services/loss_autopsy.py` (LLM path) | No LLM for compliance — replaced with deterministic `audit_report.py` |
| `src/tasks/golden_set_eval.py` | Challenger evaluation — Sprint 2 |
| `src/services/lifecycle_suppression.py` | FA's cross-table sibling lookup doesn't map to Blackink's single-contacts model |
| `src/services/sms_send_log.py` | SMS send log table not yet provisioned — Week 1 |
| `src/services/telnyx_sms.py` | A2P 10DLC filing in progress — Week 1 after registration clears |
| All FA billing/Stripe services | Blackink billing model different (entitlement_offers rows, not FA's pricing_cohorts) — Sprint 2 |

---

## Acceptance Criteria Coverage

| AC | Owner | Module(s) | Status |
|---|---|---|---|
| AC #1 — Live Slack cockpit + hash verification | Dev 3 | `@Blackink` app + Block Kit handlers | Input pending from Dev 3 |
| AC #2 — Vera zero silent-zero returns | Dev 2 | Vera data-reconciliation refactor | Input pending from Dev 2 |
| AC #3 — Persistent halt survives restarts | Dev 2 | Relay state machine refactor | Input pending from Dev 2 |
| AC #4 — Hunter entity resolution batch passes | Dev 2 | Hunter standalone worker | Input pending from Dev 2 |
| AC #5 — CI/CD SMS gate live | Dev 4 | `cold_sms_gate.py`, `sms_quiet_hours.py`, `outbound_templates.py` | ✅ 100 tests passing |
| AC #6 — Signed data contract + staging schema | Dev 1 | `apply_raw_prospect_pipeline.py`, quarantine gate, Akrash grant | Input pending from Dev 1 |
| AC #7 — Reuse Ledger delivered | Dev 4 | This document | ✅ |
| A2P 10DLC submitted | Parked | Non-dev / compliance (Telnyx/TCR portal) | Filed separately |

---

*Dev 4 section: complete, 100 tests, committed to `dev4/compliance-gates-suppression`.*  
*Dev 1–3 sections: framework compiled from sprint spec + FA codebase analysis. Awaiting each dev's test counts and exact file names for final Gate 1 sign-off.*
