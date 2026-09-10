# Week 1-2 Reuse Ledger — Supplement to Week 0

**Compiled:** 2026-09-10, from the current codebase (`CLAUDE.md`'s Architecture
section and module docstrings) and this session's own PR
(`feature/audit-spec-gaps`, https://github.com/jbkcreator/blackink/pull/50).
Closes audit rows **3.0.1 / 3.0.6 #7** ("no ledger artifact exists as
source-verifiable output" beyond Week 0) for Week 1-2 — same accounting
convention as `docs/reuse_ledger_week0.md` (Type / source / key changes /
tests), continued forward rather than rewriting that file, since it was
itself a dated Gate-1 sign-off deliverable for a different period.

**Scope note:** this is a supplement, not a replacement — read alongside
`reuse_ledger_week0.md`. It covers the major Week 1 and Week 2 subtasks that
actually shipped, plus this session's own audit-remediation work
(S-1, S-2, S-8, S-11, D-6, D-7, the `@Blackink` mention router). It does not
re-litigate Week 0's own ledger entries.

---

## Table of contents

1. [Week 1 — Inbound Booking Engine (Subtask 3.2.1)](#week-1--inbound-booking-engine-subtask-321)
2. [Week 1 — Show-Rate Reminder Cascade (Subtask 3.2.2)](#week-1--show-rate-reminder-cascade-subtask-322)
3. [Week 1 — No-Show Handler & Self-Serve Landing Page (Subtask 3.2.3)](#week-1--no-show-handler--self-serve-landing-page-subtask-323)
4. [Week 1 — Post-Booking "Log Outcome" Trigger Card (Addendum)](#week-1--post-booking-log-outcome-trigger-card-addendum)
5. [Week 2 — Owner Enrichment / Skip-Trace (Subtask 3.2.1)](#week-2--owner-enrichment--skip-trace-subtask-321)
6. [Week 2 — Appointment Operations (Subtask 1.1.1)](#week-2--appointment-operations-subtask-111)
7. [Week 2 — Zero-Deposit Card Auth & ACH Mandate Capture (Subtask 1.2.1)](#week-2--zero-deposit-card-auth--ach-mandate-capture-subtask-121)
8. [Week 2 — 50/50 Settlement Split Engine & Clawback Monitor (Subtask 1.2.2)](#week-2--5050-settlement-split-engine--clawback-monitor-subtask-122)
9. [Week 2 — Six Billing Rules (Subtask 1.2.3)](#week-2--six-billing-rules-subtask-123)
10. [This session — Week 0-2 Audit Remediation (S-1, S-2, S-8, S-11, D-6, D-7, mention router)](#this-session--week-0-2-audit-remediation)
11. [FA Modules Explicitly Not Ported (Week 1-2)](#fa-modules-explicitly-not-ported-week-1-2)
12. [Known Gaps Carried Forward](#known-gaps-carried-forward)

---

## Week 1 — Inbound Booking Engine (Subtask 3.2.1)

**Type:** New build, pattern-only reuse from Week 0's tenant-isolation
scaffolding (`config/tenant_policies.py`, `session_scope`) — no direct FA
port; FA has no equivalent client-owned-calendar booking flow.

- `src/api/booking_webhook_router.py` — Google/Microsoft webhook handshake
  validation + fast enqueue into `calendar_sync_queue`; `src/services/ghl_webhook.py`
  — GoHighLevel fallback, real (not stubbed), per-client shared-secret auth
  since GHL has no OAuth app of its own.
- `src/services/booking_ingest.py` — `sync_connection_locked` (Postgres
  advisory lock, serialized per connection), shared `_process_event` pipeline
  across all three providers (Google/Microsoft/GHL) — one implementation, not
  three.
- `owner_contacts` is a **new** table, deliberately distinct from `contacts`
  (capped at two PM-firm staff roles per company) — not a Week-0 port.
- **Hard constraint honored, not a code pattern but a client directive
  (W1-8):** "Blackink never books into a Blackink calendar" — no shared/
  pooled calendar anywhere in this flow, no Calendly.

**Tests:** `tests/test_booking_webhook_router.py`, `tests/test_booking_ingest.py`,
`tests/test_ghl_webhook.py`.

## Week 1 — Show-Rate Reminder Cascade (Subtask 3.2.2)

**Type:** Pattern adoption from `calendar_confirmation.py`'s existing
`SKIP LOCKED` claim-and-send pattern (Week 1's own prior work, not FA) —
extended, not re-invented, for a second job table.

- `src/services/show_rate_reminders.py` — claim query takes an explicit
  `claim_time`/`as_of` parameter (never `NOW()`), the same discipline this
  whole codebase uses everywhere a clock needs to be fast-forwardable in
  tests (first established in Week 0's Relay halt persistence work).
- `booking_reminder_jobs` — new table, two rows per `INTERNAL_SALES_DEMO`
  booking (`24h_email`, `30min_email`).
- OVS PDF fetch (`_fetch_ovs_pdf`) reuses the SSRF-guard *pattern* Week 2's
  `owner_enrichment.py`/`self_serve_audit_worker.py` later formalized into a
  shared `_PinnedIPAdapter` — this module's own allowlist/timeout/size-cap
  logic predates that extraction and was one of the two places it was first
  written independently before being generalized.

**Tests:** `tests/test_show_rate_reminders.py`.

## Week 1 — No-Show Handler & Self-Serve Landing Page (Subtask 3.2.3)

**Type:** New build (no-show/recovery), pattern reuse (SSRF guard, rate
limiter) from Week 0's compliance-gate fail-closed posture.

- `src/services/booking_link.py`, `src/tasks/no_show_recovery_sender.py` —
  no-show pause/resume via two new `SECURITY DEFINER` functions in
  `apply_bookings.py`, same caller-scope-gated pattern as `apply_bookings.py`'s
  own `resolve_sales_demo_target()`.
- `src/services/owner_visibility/signals/website.py`'s SSRF guard
  (DNS-rebinding-safe, pinned-IP fetch) was **first written here**, for the
  self-serve landing page's public, untrusted-input Google Places call — then
  reused, not duplicated, by Week 2's Owner Enrichment worker once that
  subtask made the same provider reachable from a second untrusted-input
  path.
- Redis-backed per-IP-hash rate limiter fails **closed** (an outage rejects
  the request, HTTP 503) — same posture as `EMAIL_SENDING_ENABLED` defaulting
  `False` in Week 0's email-dispatch gate; explicitly modeled on it, not a
  new invention.

**Tests:** `tests/test_no_show_prompts.py`, `tests/test_no_show_recovery_dispatch.py`,
`tests/test_public_landing_router.py`.

## Week 1 — Post-Booking "Log Outcome" Trigger Card (Addendum)

**Type:** Pattern adoption — reuses Week 0's `src/services/slack/payload_hash.py`
SHA-256 hash-binding scheme unchanged, rather than a second implementation.

- `src/services/meeting_outcome_prompts.py` — third sibling to
  `schedule_show_rate_reminders()`/`schedule_no_show_prompt()` (same
  insert/reschedule/cancel lifecycle already established for the reminder
  cascade above).
- Two new `SECURITY DEFINER` mirror functions
  (`mirror_contact_objections`/`mirror_company_intelligence`) — same
  caller-scope-gated pattern as `pause_contact_after_no_show()`, closing a
  pre-existing silent no-op this addendum discovered (a plain `UPDATE`
  matching zero rows under RLS) — also silently fixed the identical gap in
  3.2.3's own `mark_no_show` path as a side effect.

**Tests:** `tests/test_meeting_outcome_prompts.py`.

## Week 2 — Owner Enrichment / Skip-Trace (Subtask 3.2.1)

**Type:** New Tracerfy integration (skip-trace product, distinct contract
from the Week-0 DNC-scrub product on the same vendor account); ABC pattern
directly modeled on Week 0's `compliance_gate.py` `DncProvider`/
`EmailVerificationProvider` split.

- `src/services/owner_enrichment.py` — `submit()`/`collect()` split forced
  by Tracerfy's own submit-then-poll API shape (up to 10 minutes), not a
  style choice.
- `src/services/tracerfy_client.py`'s `poll_skiptrace_queue()` — cross-checked
  against the working, production `ForcedAction-System/Forced-action-/src/services/tracerfy_batch.py`
  reference integration (same vendor, same account type) — the one
  significant direct FA cross-reference in this supplement, though the
  Blackink implementation itself is new code, not a port (different API
  product/contract).
- Claim-lease pattern (`enrichment_claimed_at`, `_CLAIM_LEASE_MINUTES=15`)
  matches `self_serve_audit_worker.py`'s own lease pattern from Week 1 —
  explicit, deliberate reuse of an existing in-repo pattern, cited as such
  in the module's own docstring.

**Tests:** `tests/test_owner_enrichment.py`, `tests/test_enrichment_verification*.py`,
`tests/test_tracerfy_client.py`.

## Week 2 — Appointment Operations (Subtask 1.1.1)

**Type:** Deliberate, documented deviation from the printed blueprint DDL
(Source D p5's `009_appointment_ops.sql`) — not a straight port. The
blueprint's own `client_id UUID REFERENCES companies(company_id)` doesn't
match this schema's real types anywhere; split into three real columns
(`client_id`/`company_id`/`contact_id`) matching Subtask 1.1.1's own stated
reasoning in `CLAUDE.md`.

- `src/services/appointment_state.py` — pure state-transition functions
  (reschedule capped at 2, `opportunity_id` retained across reschedule/
  no-show-recovery) — new code, no FA equivalent (FA has no appointment
  state machine of this shape).
- `appointments.is_billable` — STORED generated column, schema-level slice
  of the billing gate; the transition-guard logic a generated column can't
  express lives in `src/services/appointments.py` instead — same
  schema/service split pattern as Week 0's `is_clawed_back` (this repo's own
  precedent, not FA's).

**Tests:** `tests/test_appointment_state.py`, `tests/test_appointments_live.py`.

## Week 2 — Zero-Deposit Card Auth & ACH Mandate Capture (Subtask 1.2.1)

**Type:** Greenfield Stripe integration — no Stripe usage existed anywhere
in this repo before this subtask; no FA equivalent (FA has no
zero-deposit/dual-SetupIntent flow).

- `src/core/token_crypto.py` (Fernet encrypt/decrypt) — **reused**, not
  duplicated, from its existing Week-0/Week-1 use for calendar OAuth tokens
  and SMTP passwords; this subtask is its third consumer, not a second
  encryption helper.
- `src/services/payment_auth_token.py` — new signed-token pattern for
  pre-portal onboarding auth, structurally similar to (but a distinct
  secret/purpose from) `src/services/calendar_oauth.mint_calendar_connect_link`'s
  single-use signed link from Week 1 — same "no client-portal login exists
  yet" scaffolding-token idea, applied a second time, not shared code.

**Tests:** `tests/test_payment_auth_router.py`, `tests/test_payment_auth.py`.

## Week 2 — 50/50 Settlement Split Engine & Clawback Monitor (Subtask 1.2.2)

**Type:** New build; the one explicit tri-state-stub-provider pattern reuse
from Week 0 is direct and named in-code.

- `src/services/pms_sync.py`'s `PmsProvider` ABC + `StubPmsProvider` —
  **explicitly modeled** on `compliance_gate.py`'s `DncProvider`/
  `StubDncProvider` tri-state convention (both docstrings say so).
- `src/services/settlement/gateway.py`'s `StripeGateway` ABC — the pattern
  Subtask 1.2.3's billing rules later reused directly (`sit_invoice.py`
  calls the same gateway, not a second one) — this subtask is the origin,
  not a follow of, that abstraction.
- Evidence Packet PDF (`evidence.py`/`evidence_pdf.py`) — new; states real
  gaps verbatim (`ABSTAIN` for no DNC vendor, `NOT RECORDED` for missing
  data) rather than fabricating a plausible value, matching the whole
  codebase's fail-closed philosophy but built fresh for this document shape.

**Tests:** `tests/test_settlement_live.py`, `tests/test_no_upfront_charge_paths.py`,
`tests/test_settlement_clawback_pms_outage.py`.

## Week 2 — Six Billing Rules (Subtask 1.2.3)

**Type:** New build on top of 1.2.2's ledger/gateway; one direct reuse
of an existing pattern, one deliberate schema-location deviation.

- `src/services/billing/sit_invoice.py::charge_sit_for_appointment()` calls
  the **same** `StripeGateway.add_invoice_item()` Subtask 1.2.2 built — no
  second Stripe gateway anywhere in this codebase, confirmed structurally by
  `tests/test_billing_structural.py`.
- `founding` lives on `clients`, not `companies` — a documented deviation
  from the printed spec's own phrase, tracing to the same
  `client_id → companies` conflation Subtask 1.1.1 already resolved (cited
  explicitly in `CLAUDE.md` as the same bug class, not independently
  re-discovered).
- `src/services/clients.py::provision_client()` — the one write path for a
  `clients` row, `founding` a required keyword with no default — new code,
  no FA equivalent (FA's client model has no `founding` concept).

**Tests:** `tests/test_billing_structural.py`, `tests/test_billing_live.py`,
`tests/test_billing_sweep_gate.py`.

## This session — Week 0-2 Audit Remediation

**Type:** Bug fixes + new small features against `docs/Blackink_Week0-2_Implementation_Audit_10-09-2026_2.md`'s
own findings, landed in `feature/audit-spec-gaps` → PR #50.

- **S-1 (Vera health gate)** — new `src/agents/vera/health_gate.py` +
  `src/tasks/vera_health_sweep.py`; reused the existing, already-correct
  tri-state `HealthResult` type from Week 0 unchanged — the gap was purely
  "nothing calls it," not a logic defect.
- **S-2 (Cora 24h throttle)** — extended `src/agents/cora/throttle.py`'s
  existing Redis counter pattern with a parallel FIFO timestamp list; no new
  pattern, an addition to Week 0's own module.
- **S-8 (tracking pixel/click/email_replied)** — `src/services/email_tracking.py`
  is a direct structural port of `src/services/email_unsubscribe.py`'s
  mint/verify/url shape (Week 0/1 pattern), cited as such in its own
  docstring.
- **S-11 (real reply-send + Book Meeting)** — reused `get_active_mailbox_for_client()`/
  `build_email_sender()` from Week 1's `mailbox_dispatcher.py`/
  `email_sender.py` unchanged; no new sending primitive.
- **D-6/D-7 (compliance CI + SMS dispatch bug)** — found and fixed a real
  production bug (dead denormalized `contacts` columns) by consolidating
  onto `cold_sms_gate.py`'s existing events-ledger helpers, which had zero
  production callers before this fix (a genuine "orphan helper" of Week 0's
  own making, closed here).
- **`@Blackink` mention router** — new `app_mention` handler reusing
  `daily_digest.build_digest_text()`/`halt_service.get_active_halts()`
  unchanged, per this session's own "reuse existing architecture" review.

**Tests:** `tests/test_vera_health_gate.py`, `tests/test_vera_health_sweep.py`,
`tests/test_cora_throttle.py`, `tests/test_email_tracking*.py`,
`tests/test_sales_reply_send.py`, `tests/test_slack_mention_router.py`,
`tests/test_campaign_readiness_gate.py`, `tests/test_sms_dispatch.py`,
`tests/test_cold_sms_gate.py` — full detail and pass counts in
`docs/plans/2026-09-10-consolidated-work-log.md`.

## FA Modules Explicitly Not Ported (Week 1-2)

| FA module / concept | Reason deferred |
|---|---|
| Any Gmail/Microsoft Graph mailbox OAuth access | Client directive (Source of Truth §1.5): "Do not build Gmail or Microsoft Graph mailbox access." Ingestion is forwarding-to-alias only. |
| Twilio inbound SMS webhook, SMS send paths generally | Client directive (Source of Truth §1.3): "No SMS this year." `cold_sms_gate.py`/`sms_quiet_hours.py` remain as blocks with nothing to gate. |
| FA's `venture_ladder.py` growth-under-uncertainty bias | Inverted deliberately for `compliance_gate.py` — a compliance gate's bias must be the opposite of a growth-ladder's; cited in that module's own docstring since Week 0, unchanged here. |
| FA's cross-table sibling lookup (`lifecycle_suppression.py`) | Doesn't map to Blackink's single-`contacts` model — decided at Week 0, still true. |
| Sendspark video, `{video_url}` merge tag, watch-percentage tracking | Client directive (Source of Truth §1.2): Sendspark provisioning is out of scope; ticket 17 (provision Sendspark) not actioned. |
| `market_metro` schema/algorithms | Client directive (Source of Truth §1.4): "There is no metro layer" — county is the unit everywhere; `{city}` merge tag remapped to county in Week 1's sequence content. |

## Known Gaps Carried Forward

Genuinely unbuilt, blocked on a missing external vendor contract or client
deliverable — not a code gap, confirmed by reading `config/settings.py` and
the relevant service module, not assumed:

- **Hunter registered-agent lookup** (3.0.2 D / 3.0.6 #4) — no
  OpenCorporates/SunBiz vendor contracted.
- **B1 — email verification** (3.0.5) — no vendor key, no real
  `EmailVerificationProvider` implementation anywhere.
- **B3 — 20-domain/40-mailbox inventory** (3.0.5 / S-22) — naming
  convention question already answered (none needed — `firstname@domain`
  works, zero string logic on the address); the actual inventory (which
  domains, DNS delegation) is a client deliverable.
- **`#blackink-economics`** (3.0.3 / X-2) — scope itself is undefined by any
  Week 0-2 acceptance criterion; the full mission additionally needs Google
  Ads/Meta Marketing API credentials that don't exist in settings.
- **A2P 10DLC filing evidence** (3.0.4) — a TCR filing, not a code artifact;
  brand confirmed under HEU AI LLC, filing/approval status outstanding.

Cross-reference `docs/plans/2026-09-10-consolidated-work-log.md` for the
full evidence trail (test names, live-DB verification runs, exact pass
counts) behind every claim in this document's "This session" section.
