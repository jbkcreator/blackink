# CLAUDE.md

Guidance for working in this repository.

## Project Overview

Blackink is a B2B lead-gen/growth SaaS for property-management firms (HEU AI
LLC). It is a fork of an existing project, Forced Action, reusing its
patterns and some agent code (Cora, Vera, Hunter, Relay) — but built fresh
for strict multi-tenant `client_id` isolation across many paying customers,
which Forced Action never had. See the Data Infrastructure & Pipeline
design plan for the full schema design and rationale.

## Common Commands

```bash
# API server
uvicorn src.api.main:app --reload --port 8000

# Migrations (idempotent scripts, no Alembic) — run in this order:
PYTHONPATH=. python migrations/apply_db_roles.py
PYTHONPATH=. python migrations/apply_counties.py
PYTHONPATH=. python migrations/apply_raw_assessor_parcels.py   # Subtask 3.1.1 — Akrash staging feed, not tenant-bearing, any time after counties
PYTHONPATH=. python migrations/apply_area_code_timezones.py
PYTHONPATH=. python migrations/apply_clients.py
PYTHONPATH=. python migrations/apply_clients_stl_fields.py  # Task 4.2.1 — inbound webhook secret, subdomain slug, STL reply template
PYTHONPATH=. python migrations/apply_relay_halts.py   # not tenant-bearing, any time after clients
PYTHONPATH=. python migrations/apply_companies.py
PYTHONPATH=. python migrations/apply_contacts.py
PYTHONPATH=. python migrations/apply_pm_profiles.py
PYTHONPATH=. python migrations/apply_owner_entities.py
PYTHONPATH=. python migrations/apply_raw_prospect_pipeline.py
PYTHONPATH=. python migrations/apply_events.py
PYTHONPATH=. python migrations/apply_sandbox_dashboard_view.py   # read-only view over companies+events, not tenant-bearing — safe any time after both
PYTHONPATH=. python migrations/apply_compliance_gate_audit.py
PYTHONPATH=. python migrations/apply_campaign_readiness_gate.py
PYTHONPATH=. python migrations/apply_sms_dispatch_log.py
PYTHONPATH=. python migrations/apply_sending_domains.py
PYTHONPATH=. python migrations/apply_mailbox_last_used.py    # adds mailboxes.last_used_at (LRU rotation) — after sending_domains
PYTHONPATH=. python migrations/apply_agent_work_orders.py   # Dev 3 — before RLS, after clients
PYTHONPATH=. python migrations/apply_meeting_outcomes.py    # adds meeting_outcomes table
PYTHONPATH=. python migrations/apply_contacts_prospect_objections.py
PYTHONPATH=. python migrations/apply_sequence_runs.py       # Dev 3 — after agent_work_orders
PYTHONPATH=. python migrations/apply_sequence_touch_dispatches.py  # Dev 3 — after sequence_runs
PYTHONPATH=. python migrations/apply_companies_google_place_id.py  # adds google_place_id to companies
PYTHONPATH=. python migrations/apply_ghost_shopper_cleanup.py      # removes deferred ghost-shopper columns; renames audit_pdf_url -> ovs_pdf_url
PYTHONPATH=. python migrations/apply_owner_visibility_scores.py    # OVS engine scoring table
PYTHONPATH=. python migrations/apply_ovs_audit_requests.py         # self-serve OVS audit intake; not tenant-bearing, no RLS
PYTHONPATH=. python migrations/apply_admin_users.py                # internal dashboard admin accounts; not tenant-bearing, no RLS
PYTHONPATH=. python migrations/apply_mailbox_smtp_credentials.py
PYTHONPATH=. python migrations/apply_owner_contacts.py
PYTHONPATH=. python migrations/apply_calendar_connections.py
PYTHONPATH=. python migrations/apply_bookings.py
PYTHONPATH=. python migrations/apply_booking_reminder_jobs.py      # Subtask 3.2.2 — Show-Rate Reminder Cascade
PYTHONPATH=. python migrations/apply_no_show_prompt_jobs.py        # Subtask 3.2.3 — No-Show Handler
PYTHONPATH=. python migrations/apply_no_show_recovery_jobs.py      # Subtask 3.2.3 — No-Show Handler
PYTHONPATH=. python migrations/apply_self_serve_audit_submissions.py  # Subtask 3.2.3 — Owner Score Self-Serve Landing Page
PYTHONPATH=. python migrations/apply_meeting_outcome_prompt_jobs.py   # Addendum 3.2.1 — "Log Outcome" trigger card (needs bookings + calendar_connections; before RLS)
PYTHONPATH=. python migrations/apply_appointment_ops.py   # Subtask 1.1.1 — appointments/confirmation_logs/dispositions/disputes + state enum (needs clients+companies+contacts; before RLS)
PYTHONPATH=. python migrations/apply_inbound_messages.py  # Reply Triage Agent intake table; before RLS
PYTHONPATH=. python migrations/apply_inbound_messages_sla.py  # SLA/claim/escalation columns for context cards (Subtask 2.1.2); before RLS
PYTHONPATH=. python migrations/apply_respond_routing_gaps.py  # requires_human_review on inbound_messages; HALTED status on sequence_runs (Subtask 2.1.1)
PYTHONPATH=. python migrations/apply_inbound_messages_lead_fields.py  # Task 4.2.1 — Speed-to-Lead columns on inbound_messages (additive; after the three inbound_messages migrations, before RLS)
PYTHONPATH=. python migrations/apply_winback_imports.py   # Subtask 3.1.1 — Lost-Owner CSV Ingest (winback_imports/winback_rows; before RLS)
PYTHONPATH=. python migrations/apply_winback_touch_sequence.py   # Subtask 3.1.2 — Three-Touch Win-Back Sequence (winback_touch_dispatches, stop columns, calendar_connections.is_default_owner_booking; after apply_winback_imports.py and apply_calendar_connections.py, before RLS)
PYTHONPATH=. python migrations/apply_winback_enrichment.py   # Subtask 3.2.1 — owner-enrichment (skip-trace) columns on winback_rows; not tenant-bearing (adds columns to an already-registered table), any time after apply_winback_touch_sequence.py, before RLS
PYTHONPATH=. python migrations/apply_rls_policies.py   # run LAST
# NOTE: apply_ghost_shopper_columns.py lives on feat/agent-ghost-shopper-sub only — NEVER run on this DB
PYTHONPATH=. python migrations/apply_akrash_grant.py    # run after RLS

# Background jobs
python -m src.tasks.promotion_sweep
python -m src.tasks.county_allocation_reassessment
python -m src.tasks.deliverability_sentinel
python -m src.tasks.hunter_nightly_sweep
python -m src.tasks.calendar_subscription_renewal
python -m src.tasks.calendar_sync_worker
python -m src.tasks.booking_confirmation_sender
python -m src.tasks.show_rate_reminder_sender
python -m src.tasks.no_show_prompt_sender
python -m src.tasks.no_show_recovery_sender
python -m src.tasks.self_serve_audit_worker
python -m src.tasks.meeting_outcome_prompt_sender
python -m src.agents.respond.worker        # Reply Triage Agent classifier worker
python -m src.tasks.respond_sla_sweep      # SLA escalation sweep (HOT_LEAD/WHALE_OWNER=15min, others=60min; tier3 reallocates at 240min)
python -m src.tasks.sequence_sweep              # Dev 3 — posts due email-touch approval cards to Slack
python -m src.services.work_orders --sweep --client-id <id>  # Dev 3 — executes APPROVED touch dispatches
python -m src.tasks.enrichment_verification --client-id <id> [--import-id <id>] [--limit 10]  # Subtask 3.2.1 — owner-enrichment (skip-trace) sweep; must run before a Win-Back import can be armed, posts the pre-pilot summary to #blackink-qa

# Tests
pytest tests/                       # unit tests, no DB required for most
pytest tests/test_tenant_isolation.py  # requires a live Postgres with migrations applied
```

## Local development database

Schema/migration work must be developed and verified against a disposable
local Postgres, never against the live server — there is no separate
staging database yet, so the server's database is effectively production.

Which env file gets loaded is controlled by the `ENV_FILE` shell variable
(`config/settings.py`, default `.env`) — **never overwrite your real `.env`
to test locally.** Create a permanent `.env.local` once (gitignored via
`.env*` in `.gitignore`) and point `ENV_FILE` at it for the duration of
your shell session instead. This eliminates the backup/restore-`.env`
dance entirely — there's nothing to accidentally leave in the wrong state.

```bash
# One-time: create .env.local with the Docker test values
cat > .env.local <<'EOF'
DATABASE_URL=postgresql://postgres:localdevpass@localhost:5433/blackink
DATABASE_URL_APP=postgresql://blackink_app:app_local_pw@localhost:5433/blackink
DATABASE_URL_SYSTEM=postgresql://blackink_system:system_local_pw@localhost:5433/blackink
DATABASE_URL_AKRASH=postgresql://akrash_ingest:akrash_local_pw@localhost:5433/blackink
BLACKINK_APP_DB_PASSWORD=app_local_pw
BLACKINK_SYSTEM_DB_PASSWORD=system_local_pw
AKRASH_INGEST_DB_PASSWORD=akrash_local_pw
EOF

# Start a disposable local Postgres (port 5433, not 5432 — avoids clashing
# with a native Postgres install some dev machines already have on 5432)
docker compose up -d postgres-test

# Point this shell at .env.local for the rest of the session (PowerShell:
# $env:ENV_FILE=".env.local"; bash: export ENV_FILE=.env.local)
export ENV_FILE=.env.local

# Then run the full migration sequence from Common Commands above, and:
pytest tests/

# Tear down when finished:
docker compose down -v postgres-test
unset ENV_FILE   # or just open a fresh shell for real-.env work
```

Only once a change is verified this way should it be applied to the real
server (manual sync today — no CI/CD deploy pipeline exists yet).

## Cloud Run deployment (test)

A test-only deployment target for verifying the Google/Microsoft/GHL
booking-engine webhooks against real providers (their push-notification
APIs need a real, HTTPS-reachable URL — `localhost` doesn't work; see
Subtask 3.2.1). Not a production deploy pipeline — that remains manual
sync per above.

`Dockerfile` runs `uvicorn src.api.main:app --host 0.0.0.0 --port
${PORT}` — Cloud Run injects `PORT`, never hardcode a port. On startup,
`src/api/main.py`'s lifespan spawns the scheduled-worker threads
(`calendar_sync_worker.drain_queue`/`sweep_all_active_connections`,
`booking_confirmation_sender.run_sweep`,
`calendar_subscription_renewal.run_renewal_sweep`, and
`show_rate_reminder_sender.run_sweep` — the last is the Subtask 3.2.2
reminder cascade's only sender, so it MUST stay in
`_start_background_workers()` or every 24h/30min reminder job silently
never sends) — this only actually keeps running under an
**instance-based** billing / **min-instances ≥ 1** Cloud Run
configuration; request-based billing suspends the container (and these
threads) between requests, which would silently break the whole point of
a background worker.

**Required environment variables / secrets** (exact names
`config/settings.py` reads — set these as Cloud Run env vars or Secret
Manager-backed env vars, never baked into the image):

- `DATABASE_URL`, `DATABASE_URL_APP`, `DATABASE_URL_SYSTEM`,
  `DATABASE_URL_AKRASH` — for Cloud SQL's Unix-socket connector (not a
  TCP host/port), each DSN's format is
  `postgresql://USER:PASSWORD@/DBNAME?host=/cloudsql/PROJECT:REGION:INSTANCE`
  — the empty host before `@/DBNAME` is deliberate (psycopg2 reads the
  socket directory from the `host` query param instead). Requires the
  Cloud SQL Auth Proxy sidecar/connector enabled on the service (`--add-cloudsql-instances`).
- `BLACKINK_APP_DB_PASSWORD`, `BLACKINK_SYSTEM_DB_PASSWORD`,
  `AKRASH_INGEST_DB_PASSWORD` — only read by `apply_db_roles.py` at
  provisioning time, but keep them set consistently with the DSNs above.
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
  `MICROSOFT_OAUTH_CLIENT_ID`, `MICROSOFT_OAUTH_CLIENT_SECRET` — from
  the Google/Microsoft OAuth app consoles; the redirect URI registered
  there must exactly match `{CALENDAR_WEBHOOK_BASE_URL}/api/v1/calendar/oauth/callback/{provider}`.
- `TOKEN_ENCRYPTION_KEY` — a `Fernet.generate_key()` output, generated
  once and stored in Secret Manager, never regenerated per deploy (that
  would make every previously-encrypted token undecryptable).
- `CALENDAR_OAUTH_STATE_SECRET` — any random secret string, signs
  connect-link/state JWTs.
- `EMAIL_UNSUBSCRIBE_SECRET` — any random secret string, signs one-click
  unsubscribe JWTs (`src/services/email_unsubscribe.py`). Its own
  dedicated secret, never a reuse of `ADMIN_JWT_SECRET` — a missing value
  means every outbound cold/win-back send fails loudly at dispatch time
  rather than shipping without the mandatory unsubscribe mechanism.
- `CALENDAR_WEBHOOK_BASE_URL` — the Cloud Run service's own public
  `https://...run.app` URL (or a mapped custom domain), used to build
  both the OAuth redirect URI and the webhook URLs handed to Google/
  Microsoft/GHL at subscribe time.
- `EMAIL_SENDING_ENABLED` — leave `False`/unset unless real SMTP
  credentials for a validated sending domain exist; see
  `src/services/email_dispatch.py`.
- `OVS_PDF_ALLOWED_HOSTS` — comma-separated exact hostnames the 30-minute
  pre-demo reminder is allowed to fetch `contacts.ovs_pdf_url` from (e.g.
  the real object-storage host once Dev 2's storage step exists); leave
  unset until then — every such reminder job stays `BLOCKED` rather than
  fetching from an unapproved host. See
  `src/services/show_rate_reminders._fetch_ovs_pdf`.
- `AKRASH_INGEST_JWT_SECRET`, `TRACERFY_API_KEY` (renamed from
  `DNC_VENDOR_API_KEY` — Subtask 3.2.1 made it dual-purpose: DNC scrub and
  skip-trace owner enrichment, same Tracerfy account), etc. — the existing
  Week 1 settings, only needed if those code paths are actually
  exercised in this test deployment.

Run the standard migration sequence from Common Commands against the
Cloud SQL instance (via `psql` through the Auth Proxy, or a one-off
Cloud Run Job using the same image) before the service handles traffic
— `apply_rls_policies.py` in particular must run before any tenant data
is written, same as local dev.

## Architecture

### Tenant isolation — the core invariant
Every tenant-bearing table is registered in `config/tenant_policies.py`
(`TENANT_POLICIES`) and enforced by **both** Postgres Row-Level Security
(the non-negotiable backstop — holds regardless of how a query was
written) **and** app-layer scoping (`src/core/database.py`'s
`session_scope(client_id=...)`, which issues `SET LOCAL
app.current_client_id`). RLS is `FORCE`d, not just `ENABLE`d. A new
tenant-bearing table MUST be added to `TENANT_POLICIES` and pushed through
`migrations/apply_rls_policies.py`, or it gets neither enforcement nor
leakage-test coverage — there is no automatic net.

Three Postgres roles (`migrations/apply_db_roles.py`): `blackink_app`
(RLS-subject, the normal runtime connection), `blackink_system`
(BYPASSRLS, internal batch jobs ONLY — never imported from `src/api/`),
`akrash_ingest` (INSERT-only on the two `raw_prospect_*` staging tables).

### Two distinct ownership/suppression mechanisms — do not conflate
- `client_pm_books` — a client's real, PMS-synced managed portfolio.
  Permanent, no expiry. This is what `is_claimed_by_other_client()` (the
  non-poach SQL function) queries.
- `county_allocations` — prospect-pool outreach-rights assignment, 30-day
  reassessment window. Exactly one active row per county (partial unique
  index). `companies.owning_client_id` is a materialized reflection of the
  county's current allocation, refreshed by
  `src/tasks/county_allocation_reassessment.py`.

### `company_id` — deterministic, never random
`company_id` is `sha256(normalized_domain)`, computed in application code
(`BaseIngestLoader.compute_company_id`) — **never** `gen_random_uuid()`.
The blueprint's own raw SQL contradicts its own prose on this point; a
random UUID would silently defeat the global-dedup design. See
`src/core/models.py`'s `Company` docstring.

### Ingestion pipeline
Akrash writes directly into `raw_prospect_companies` /
`raw_prospect_contacts` (DB role, not an API — see
`src/loaders/akrash_loader.py`). `src/tasks/promotion_sweep.py` (runs every
few minutes, BYPASSRLS) validates, dedupes, and promotes cleared rows into
the canonical `companies`/`contacts` tables — a company can promote with
only one of its two contacts clean (confirmed behavior). Nothing is ever
silently dropped: every row that doesn't promote carries a
`reject_reason_code`.

### Compliance gate vs. quarantine gate — two different lifecycle stages
- `src/services/quarantine_gate.py` — is a freshly-ingested row eligible
  for **promotion**? (`pending/cleared/quarantined/rejected`)
- `src/services/compliance_gate.py` — is an already-promoted contact
  eligible for a **send** on a given campaign attempt?
  (`PASS/FAIL/ABSTAIN`, tri-state — `ABSTAIN` always blocks, no
  override). Both share the `DncProvider`/`EmailVerificationProvider`
  interfaces (no vendor contracted yet — stub implementations only).

### Outbound email — mandatory one-click unsubscribe (non-negotiable)
**Every cold/win-back outbound email MUST carry a working, self-service
one-click unsubscribe — no exceptions, no sequence ships without it.** This
is a client-stated hard requirement (confirmed 2026-09-07), ported from
`ForcedAction-System/Forced-action-`'s pattern
(`src/services/email_unsubscribe.py`, `src/api/email_unsubscribe_router.py`,
`docs/adr/0028-cross-channel-suppression-block-all.md` in that repo): a
stateless signed token (PyJWT — this repo's existing library, not
`python-jose`), minted once per send, embedded as both a `List-Unsubscribe`
/ `List-Unsubscribe-Post: List-Unsubscribe=One-Click` header pair (RFC 8058
— also required by Gmail/Yahoo's 2024 bulk-sender rules) and a visible link
in the email body, landing on a public unauthenticated endpoint
(`src/api/unsubscribe_router.py`, `GET`/`POST /api/v1/public/unsubscribe`)
that suppresses the recipient immediately and idempotently. The click sets
`contacts.is_opted_out = TRUE` and/or `winback_rows.stopped_at`/
`stop_reason = 'OPT_OUT'` depending which table the email matches for that
`client_id` — both are already hard gates in `compliance_gate.py` and the
win-back touch gate respectively, so no new send-blocking logic is needed
once the column is set, only the mechanism to set it. `EmailSender.send()`
takes this as a `list_unsubscribe_url` parameter so both the cold 5-touch
sequence (`sequence_orchestrator.py`) and any future outbound sequence get
it from the same shared sending layer — do not reimplement per-sequence.
See `docs/plans/2026-09-07-subtask-3.1.2-three-touch-winback-sequence.md`
§3.7 for the full design.

### Deliverability infrastructure
DNS/SPF/DKIM/DMARC setup and mailbox warmup are a **manual runbook**, not
code (matches Forced Action's own ADR 0011 call). `sending_domains` /
`mailboxes` only track state; `src/tasks/deliverability_sentinel.py`
(adapted from Forced Action's `email_deliverability_monitor.py`)
quarantines a domain and promotes a same-cluster reserve domain on a
bounce/complaint-rate trip.

### Inbound booking engine (Subtask 3.2.1)
A property owner books on the **client's own** connected calendar
(Google Calendar or Microsoft Graph — never a Blackink-owned calendar,
and no Calendly; client comment W1-8). Only events tagged
`extendedProperties.private.blackink_booking = "1"` (Google) /
the equivalent `singleValueExtendedProperties` entry (Microsoft) —
set by whichever component builds the owner-facing booking widget, not
by this subsystem — become `bookings` rows; an ordinary meeting on the
same calendar is ignored. Owner identity lives in `owner_contacts`, a
new table distinct from `contacts` (capped at two PM-firm staff roles
per prospected company) and `owner_entities` (unpopulated dedup
scaffolding) — an unmatched booking is queued
(`status='PENDING_RECONCILIATION'`) rather than a guessed
company/contact being created.

Webhook routes (`src/api/booking_webhook_router.py`) only validate the
provider handshake and enqueue into `calendar_sync_queue` — both
providers require a fast response, so the real sync
(`src/services/booking_ingest.py`'s `sync_connection_locked`, serialized
per connection via a Postgres advisory lock) runs out-of-band, via a
FastAPI `BackgroundTask` for the common case and
`src/tasks/calendar_sync_worker.py`'s scheduled sweep as the durability
backstop and periodic safety net against dropped notifications.
Confirmation email is real (not stubbed) but gated by
`settings.email_sending_enabled` (default `False`) — a missing/
misconfigured sending domain is a visible `booking_confirmation_blocked`
event, not a silent no-op. **No SMS anywhere in this flow** — per
client comment W1-4 ("we send no SMS this year"),
`src/services/booking_ingest.py` never imports
`src/services/sms_dispatch.py`.

No client-portal login system exists anywhere in this repo (no
`users`/session table). OAuth `state` binds to a single-use signed
connect-link token (`src/services/calendar_oauth.mint_calendar_connect_link`),
meant to be minted automatically by the (separate, not built here)
onboarding flow's calendar-connect step.

**GoHighLevel (GHL)** is the fallback when a client has neither a Google
nor Microsoft calendar (`src/services/ghl_webhook.py`,
`POST /api/v1/webhooks/booking/ghl/{connection_token}`) — real, not a
stub. Unlike Google/Microsoft, GHL has no OAuth app and no fetch step
(the full booking payload arrives directly in the webhook POST body,
configured as a Custom Webhook workflow action in the client's GHL
account) and no native HMAC signature scheme, so authentication is a
per-client shared secret header rather than a computed signature. Reuses
the identical booking/owner-matching/confirmation pipeline as Google/
Microsoft via `booking_ingest._process_event` — one implementation, not
three.

### Show-Rate Reminder Cascade (Subtask 3.2.2)
A second, distinct booking flow from 3.2.1 above:
`calendar_connections.connection_scope` is `CLIENT_OWNER_BOOKING` (a
property owner booking the PM-firm client's own calendar, 3.2.1) or
`INTERNAL_SALES_DEMO` (a prospective PM firm booking a sales-demo call
with an authorized Blackink sales rep's own Google/Microsoft calendar,
under the reserved `client_id='BLACKINK_INTERNAL_SALES'` row). W1-8
("Blackink never books into a Blackink calendar") governs
`CLIENT_OWNER_BOOKING`; `INTERNAL_SALES_DEMO` still uses real,
individually OAuth-authorized calendars belonging to real staff, never a
shared/pooled one and never Calendly — the two scopes differ only in
*whose* calendar receives the booking. An `INTERNAL_SALES_DEMO` booking
matches its target against `contacts`/`companies` by work email (via the
`resolve_sales_demo_target()` `SECURITY DEFINER` function — those tables
are RLS-scoped through `companies.owning_client_id`, which is `NULL` for
every unallocated prospect, so a session scoped to the internal-sales
client_id can never see them directly), never `owner_contacts`; `bookings`
carries `target_company_id`/`target_contact_id` for this scope instead of
`owner_contact_id`.

`booking_reminder_jobs` holds two rows per `INTERNAL_SALES_DEMO` booking
(`24h_email`, `30min_email`), created/rescheduled/cancelled by
`booking_ingest.schedule_show_rate_reminders()` on every insert,
reschedule, *and* cancellation of such a booking (not gated on a
new-insert check alone, or a reschedule would silently fail to move its
jobs). Fire time is an absolute `TIMESTAMPTZ` offset from
`bookings.scheduled_at` — no timezone math needed to decide *when* a job
runs. `src/tasks/show_rate_reminder_sender.py`'s sweep claims due jobs via
`src/services/show_rate_reminders.py`'s `SKIP LOCKED` pattern (mirroring
`calendar_confirmation.py`), with the claim comparison taking an explicit
`claim_time`/`as_of` parameter rather than SQL `NOW()` — what makes a
fast-forward timing test possible without a real 24-hour wait, and what
lets `send_show_rate_reminder()` refuse to send a reminder for a meeting
that's already started (`as_of >= scheduled_at` → `SKIPPED`, never a
stale send) regardless of why processing was delayed.

The 30-minute pre-demo email attaches Dev 2's **stored** Owner
Visibility Score PDF (`contacts.ovs_pdf_url`), fetched via
`show_rate_reminders._fetch_ovs_pdf()` — never regenerates one via
`pdf_report.compile_pdf()` itself, which would produce a second,
independently-generated report. `ovs_pdf_url` is untrusted input
(written by Dev 2's own, separate storage step, not this codebase), so
the fetch enforces: an exact-hostname allowlist
(`OVS_PDF_ALLOWED_HOSTS`, comma-separated — empty means fail closed,
same posture as `EMAIL_SENDING_ENABLED`), `https` only,
`follow_redirects=False` (a redirect could otherwise land outside the
allowlist), a real connect/read timeout, a 10 MiB cap enforced while
streaming (never trusting a `Content-Length` header alone), and both
`Content-Type` and the actual `%PDF-` magic bytes checked. A URL that
fails any of these marks the job `BLOCKED`/`UNSAFE_OVS_PDF_URL` — a
distinct reason from `MISSING_OVS_PDF`, and one the sweep's self-heal
step does *not* auto-promote (the row reappearing doesn't mean the
underlying safety problem got fixed). If no `owner_visibility_scores`
row or `ovs_pdf_url` exists yet for the target company (true today —
`owner_visibility_sweep.py` doesn't populate `ovs_pdf_url`, only the
compiler function exists), the job is marked `BLOCKED`
(`MISSING_OVS_SCORE` / `MISSING_OVS_PDF`) rather than sending fabricated
content; the sweep's self-heal step re-checks and auto-promotes those
two specific reasons back to `PENDING` the moment the underlying data
actually appears, no event-consumer plumbing required. `BLOCKED` rows
(any reason) and `UNCERTAIN` rows are excluded from the claim query —
never re-reclaimed every sweep tick.
No self-serve reschedule mechanism exists anywhere in this repo or its
provider contracts (Google's `htmlLink`/Microsoft's `webLink` are
event-*view* links, not reschedule actions) — the 24h email may show a
correctly-labelled "View calendar event" link, never a relabeled or
fabricated reschedule link.

### No-Show Handler & Owner Score Self-Serve Landing Page (Subtask 3.2.3)

**No-show, scoped to `INTERNAL_SALES_DEMO` only** — a PM-firm prospect
missing their Blackink sales demo, not a property owner missing a
client's own appointment (`CLIENT_OWNER_BOOKING` has no cold-outbound
concept to pause). `src/tasks/no_show_prompt_sender.py` posts a "Mark
No-Show" Slack card (`#blackink-command`) at exactly `bookings.scheduled_at`
— never before, and never after `scheduled_at + 10 minutes` (a job
claimed that late is marked `SKIPPED`, not posted, since a stale prompt
would only invite an incorrect late click). A booking whose
`target_contact_id` is still unresolved (`PENDING_RECONCILIATION`) never
gets an actionable button — its `no_show_prompt_jobs` row stays
`BLOCKED` rather than risk pausing an inferred contact.

**Blocking dependency, stated plainly:** there is no durable outbound-
sequence/campaign-enrollment engine anywhere in this codebase yet
(`src/agents/cora/worker.py`'s `_process_draft()` is an explicit Week-0
placeholder, and nothing anywhere calls `cora.queue.publish()`). "Pause
the outbound sequence" is implemented as `contacts.outbound_paused_at`/
`outbound_pause_reason`/`outbound_pause_source_booking_id`, enforced by a
new `outbound_not_paused` check in `src/services/compliance_gate.py` —
the real, already-existing choke point every cold-campaign send passes
through per contact. Once a real sequence-enrollment table exists, it
must check this column before scheduling a contact — not done yet.
`contacts` is RLS-scoped via `companies.owning_client_id` (`NULL` for
every unallocated prospect), so pausing/resuming go through two new
SECURITY DEFINER functions in `apply_bookings.py`
(`pause_contact_after_no_show`, `resume_contact_if_rebooked`) — same
caller-scope-gated pattern as `resolve_sales_demo_target()`. Resume only
fires when the contact's `outbound_pause_source_booking_id` differs from
the newly-upserted `booking_id` — a routine re-sync of the SAME
no-showed booking's webhook event must never clear the pause.

The Slack `mark_no_show` click handler (`src/services/slack/listeners.py`)
does no external I/O inside its transaction — it records the
`meeting_outcomes` `NO_SHOW` row, pauses the contact, and INSERTs exactly
one `no_show_recovery_jobs` row (`PENDING`). `src/tasks/no_show_recovery_sender.py`
sends the actual email (email-only, no SMS import anywhere in this
path), rechecking immediately before send: already-rebooked ->
`CANCELLED`; booking no longer eligible -> `SKIPPED`; sending disabled or
no booking-link redirect available -> `BLOCKED`. `triggered_at`/`sent_at`
on the job row exist specifically to prove the "within 5 minutes" DoD
line with a real assertion, not just a short sweep interval.

**Booking-page redirect** (`src/services/booking_link.py`) never guesses
— it requires an operator to flag exactly one `INTERNAL_SALES_DEMO`
`calendar_connections` row `is_default_sales_booking = TRUE` with a
manually-provisioned `public_booking_url` (a real GHL calendar's public
page, or a real Google Calendar "Appointment Schedule" page — no
self-serve slot-picker exists anywhere in this repo for Google/
Microsoft). Query-param pre-fill is only attempted for a `GOHIGHLEVEL`
connection, and only once `_GHL_PREFILL_PARAMS` in that file has been
verified against a real GHL calendar's public booking-page contract —
not assumed. Until a default connection is flagged, `resolve_booking_link()`
returns `None` and the redirect DoD line is not complete.

**Self-serve landing page** (`GET /audit`, `GET /api/v1/public/counties`,
`POST /api/v1/public/owner-score-audit` — `src/api/public_landing_router.py`)
is the one deliberately unauthenticated router in this API, scoped to
the reserved `BLACKINK_INTERNAL_SALES` client (never a bare/unscoped
session — `events` is RLS-protected direct-`client_id` mode and would
otherwise reject the log-event INSERT). The POST handler only validates
and inserts a row into `self_serve_audit_submissions` (mirrors
`raw_prospect_companies`' staging philosophy) — no live company lookup,
no Owner Visibility Score computation, no Google Places call in the
request path, so the response never depends on whether a company
already exists. Name, work email, company, and county are all required
(pydantic `EmailStr` + non-empty `Field(...)`) — a domain-only
submission carries no lead and no scoreable company context.

**County is required, not optional metadata:** both `companies.county_slug`
and `owner_visibility_scores.county_slug` are `NOT NULL` (FK to
`counties`), and this repo has no domain-to-county geocoding step — a
self-serve submission with no county can never become a `companies` row
at all, let alone a score. The landing page's county `<select>` is
populated from the real, currently-launched counties (`GET
/api/v1/public/counties`, read-only reference data, no auth needed),
never free text; the POST handler validates the submitted `county_slug`
against a real `counties` row before accepting it.
`self_serve_audit_worker.py` checks the submission's own `county_slug`
**before** ever attempting the `companies` INSERT (which would
otherwise crash on the NOT NULL constraint, not fail gracefully) and
marks a submission with none `BLOCKED_MISSING_COUNTY` — a defense-in-depth
case, not the expected path, since the API already requires it.

`src/tasks/self_serve_audit_worker.py` is the only thing that ever
creates/finds a `companies` row (using the visitor-submitted company
name and county, never the raw domain as a fake company name) or calls
`score_one_company()` (extracted from `owner_visibility_sweep.py` so the
monthly sweep and this self-serve path share one scoring implementation
— extracting it also surfaced and fixed two pre-existing, previously
untested SQL bugs: a bare `:param::jsonb` cast confuses SQLAlchemy's
bind-parameter parser and must read `:param ::jsonb` with a space) for
a self-serve submission, running BYPASSRLS same as `promotion_sweep.py`.
Retry is bounded and terminal, not silently dead-ended: a scoring
failure sets `FAILED` with an exponential-backoff `next_retry_at`
(reclaimed by the same claim query, same pattern as every other job
table in this subtask) until `_MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT` is
hit, then `FAILED_PERMANENT` (excluded from the claim query, never
retried again); a mid-scoring DB failure rolls back the session before
recording it, since a poisoned transaction would otherwise also fail
the failure-recording UPDATE itself. An already-scored-this-month
domain is marked `SKIPPED_RECENT` (bounds Google Places spend on repeat
submissions); a submitted domain that resolves to a private/loopback/
link-local/metadata IP, or is a raw IP literal, is rejected as
`REJECTED_DOMAIN` — the same SSRF guard
(`src/services/owner_visibility/signals/website.py`, re-checked at fetch
time and on every redirect hop, not just at submission time) also now
protects the pre-existing monthly sweep, since this subtask made that
provider reachable from untrusted public input for the first time. The
guard resolves the host to a validated public IP and **pins the
connection to that exact IP** via `_PinnedIPAdapter` (preserving the
hostname for TLS SNI / cert verification) — closing the DNS-rebinding
TOCTOU window where a name validated as public could resolve to
`169.254.169.254` at connect time. The fetch also streams under an
absolute wall-clock deadline and a body-size cap (not just an inactivity
timeout), so a hostile site can neither hold the single self-serve
worker forever with a slow drip nor exhaust memory with an unbounded
body.

A honeypot field (`website_url`) and a Redis-backed per-IP-hash rate
limit (`SELF_SERVE_RATE_LIMIT_PER_10MIN`) gate the endpoint. The rate
limiter fails **closed**: if Redis itself can't be reached, the request
is rejected (HTTP 503) rather than silently exempted from the limit —
same posture as `EMAIL_SENDING_ENABLED` defaulting False. `ok` in the
JSON response tells the landing page's JS whether to fire the tracking
pixels (accepted-for-processing vs. rejected-domain/honeypot/rate-
limited) — it never reveals whether the company already existed or was
already scored, the one thing this response must never leak. Meta/Google
Tag `<script>` tags are injected into the DOM only from inside the
POST's success handler (`response.ok && data.ok`) — never present in the
initial HTML, never fired on page load, and the fired events carry no
submitted PII. Navigation to the booking-link redirect is deliberately
delayed a short, bounded interval (`PIXEL_FLUSH_DELAY_MS`) after firing
the pixel calls, so the injected fbevents.js/gtag.js beacon has time to
actually be sent before the page unloads — proving that ordering is a
browser-level (Network-tab / Playwright-style) assertion this repo has
no JS test runner for, so it's a manual verification item, not an
automated test.

**Deployment, explicitly open:** `/audit` reachable on this service's own
URL is what code can verify. `audit.getblackink.com` requires a DNS
record and a Cloud Run domain mapping done outside this codebase (same
manual-runbook posture as sending-domain DNS) — not done as part of this
change.

### Post-Booking "Log Outcome" Trigger Card (Addendum to Subtask 3.2.1)

Neither spec version assigns ownership of the Slack trigger that opens
4.2.2's post-meeting outcome modal — this addendum posts that card and
wires the click. **Scoped to `INTERNAL_SALES_DEMO` only**, like the
no-show handler and for the same hard reason: `meeting_outcomes.contact_id`/
`company_id` are `NOT NULL` FKs to `contacts`/`companies`, which only a
sales-demo booking has (`target_contact_id`/`target_company_id`); a
`CLIENT_OWNER_BOOKING` links to `owner_contacts` and could never produce
an outcome row.

On every `meeting_booked` for such a booking,
`src/services/booking_ingest.py`'s `schedule_meeting_outcome_prompt()`
(third sibling to `schedule_show_rate_reminders()`/
`schedule_no_show_prompt()`, same insert/reschedule/cancel call sites and
lifecycle) upserts one `meeting_outcome_prompt_jobs` row at the meeting's
own `scheduled_at` — never a guessed end time; the click, not any timer,
signals the meeting is done. Cancelling the booking transitions the
pending job to `CANCELLED` (no outcome prompt for a meeting that never
happened). `src/tasks/meeting_outcome_prompt_sender.py` sweeps
(self-heal → claim → post → follow-up) via
`src/services/meeting_outcome_prompts.py`'s `SKIP LOCKED` pattern.

The card is a real `agent_work_orders` row, so its SHA-256 payload-hash
binding (over payload + `recipient` + config) is the existing shared
`src/services/slack/payload_hash.py` — no second implementation. The
button's `recipient` is the assigned closer's Slack id, bound into the
hash preimage; `@app.action("log_meeting_outcome")` rejects a click from
anyone else (`"assigned to a different closer"`). **The 24-hour card
expiry is deliberately NOT part of the hash** (that preimage is FIXED —
adding a timestamp window would change every digest and force a
`HASH_VERSION` bump), so it's an explicit `order.created_at + 24h` check
in both the click handler and the `@app.view` submit handler, rejected
with the same `"This action has expired or was altered"` message as an
altered card. An unclicked card gets exactly one threaded reminder ping
at `posted_at + 4h`; at `posted_at + 24h` the job goes `EXPIRED` and the
sweep stops examining it — matching how every other expired card behaves.

The card @mentions and authorizes against the assigned closer's Slack id,
**derived from the booking's own rep-calendar-slot email**
(`bookings.client_rep_email`) via `users.lookupByEmail` (bot scope
`users:read.email`) — matching the addendum's DoD, which sources the
closer from that webhook field rather than a hand-entered mapping. The
resolved id is cached onto `calendar_connections.rep_slack_user_id` so the
lookup runs at most once per rep; a value already in that column is
honored as a manual override and skips the lookup. Only when there is
neither a column override nor a resolvable rep-slot email does the job
stay `BLOCKED`/`MISSING_REP_SLACK_USER_ID` (as an unresolved
`target_contact_id` stays `UNRESOLVED_TARGET`); the sweep's self-heal step
promotes a row back to `PENDING` once the column is set (the lookup-failure
path is not auto-retried — a not-found email won't resolve on its own),
the same pattern the show-rate reminder uses for `MISSING_OVS_*`.

`open_meeting_outcome_modal()` and the global
`@app.view("meeting_outcome_submit")` submit handler are **built here**
(4.2.2's own were never built in this repo — whoever owns 4.2.2 reconciles
against this, not a second copy). The submit handler re-verifies auth,
hash and the 24h window (a view_submission is a separate request and the
modal can sit open), refuses if a NO_SHOW was already logged from the
`#blackink-command` "Mark No-Show" card (the two surfaces log the same
meeting), then in one transaction records the outcome, marks the work
order `DONE`, and logs `meeting_outcome_recorded` carrying the clicked
card's own `contact_id`.

**Fixes a pre-existing silent no-op:** `meeting_outcomes.py`'s intelligence
mirror (`contacts.prospect_objections`, `companies.current_pm_software`/
`door_count_est`) was a plain `UPDATE`, which matches **zero rows** under a
`BLACKINK_INTERNAL_SALES`-scoped session — those tables are RLS-scoped via
`companies.owning_client_id`, `NULL` for every unallocated prospect. Two
new hardened `SECURITY DEFINER` functions
(`mirror_contact_objections`/`mirror_company_intelligence`, in
`apply_meeting_outcome_prompt_jobs.py`, same caller-scope-gated pattern as
`pause_contact_after_no_show()`) do the write for that scope; the plain
`UPDATE` stays for any normally-allocated tenant. Each is a no-op for the
other's case, so exactly one writes — this also silently fixed the same
gap in 3.2.3's existing `mark_no_show` path.

The full click→modal→submit round-trip (including both intelligence
mirrors landing on the RLS-scoped rows) has been verified against a real
Slack workspace over Socket Mode. Note that **Interactivity must be toggled
on** in the Slack app config even under Socket Mode — Socket Mode only
replaces the Request URL; it does not enable interactivity, and a card's
button renders with a warning until it is on.

### Owner Enrichment / Skip-Trace (Week 2 Subtask 3.2.1 — note the number
collides with Week 1's unrelated "Inbound booking engine" section above;
these are two different subtasks that happen to share a number across
sprints)

Client mandate: *"Confirm the enrichment step (owner → phone/email) for
every signal... If it isn't, it's a blocker for everything in §3"*
(`blackink-client-comments-04-09-2026.md:77`). Skip-tracing is also written
into the Win-Back Recipe's own definition (Blueprint v2:659 — *"Runs
automated skip-tracing and DNC/suppression screening"*), so this is not
solely a client-comments-derived requirement.

`src/services/owner_enrichment.py` is the one place enrichment logic lives:
an `OwnerEnrichmentProvider` ABC (mirroring `compliance_gate.py`'s
`DncProvider`/`EmailVerificationProvider` pattern) split into `submit()`/
`collect()` rather than one call — **forced by the chosen vendor, not a
style choice**. Skip-trace and DNC are both Tracerfy, same account
(`TRACERFY_API_KEY`, renamed from `DNC_VENDOR_API_KEY` since it is now
dual-purpose), and Tracerfy's API is submit-then-poll (up to 10 minutes —
see `src/services/tracerfy_client.py`). The submit/collect split is what
lets the caller (`src/tasks/enrichment_verification.py`) commit each
chunk's `enrichment_attempts` bump *between* the two calls: a submit-stage
failure (bad key, rate limit) means nothing was queued or billed, so it
costs zero retry budget; a collect-stage (poll) failure happens after the
vendor may already be processing, so the bump must already be committed to
survive a crash without risking a silent re-charge on the next sweep.
`StubOwnerEnrichmentProvider` is the no-key fallback — it passes a row's
CSV-supplied `phone`/`email` through unchanged, so a row that already has
one keeps exactly the sequenceability it has today (the Win-Back gate below
cannot regress the moment it lands).

**`TracerfyEnrichmentProvider` is fully implemented** (2026-09-08),
cross-checked against Tracerfy's own API docs and the working, production
ForcedAction-System reference integration (same vendor, same account
type — `ForcedAction-System/Forced-action-/src/services/tracerfy_batch.py`).
Skip-trace is a *different* Tracerfy product from the DNC scrub with its
own contract: `POST /v1/api/trace/` as **multipart/form-data** (not JSON),
a `queue_id` response key, and `GET /v1/api/queue/{id}` returning the
result array **directly** (not DNC's `{"pending","download_url"}`
wrapper) — completion is a stability window (row count steady across
several polls, plus a minimum settle time), since Tracerfy streams results
in; see `tracerfy_client.py`'s `poll_skiptrace_queue()`. Two gaps Tracerfy's
API creates, both handled in `owner_enrichment.py`, not the transport layer:
no submitted-row ID is echoed back in the result (matching is by normalized
street address, reusing `winback_ingest.normalize_address()`), and `city`
is a required separate request field while `winback_rows` only stores one
freeform address string (`_split_address()` parses the confirmed
`"STREET, CITY, ST[ ZIP]"` convention only — an address that doesn't match
is excluded from that sweep's submission rather than guessed, and simply
retried next sweep; upgrade path if real CSVs need more formats is the
`usaddress`-based parser already proven in the ForcedAction-System sibling
repo). `_split_owner_name()` is a deliberately simple first-token/rest split
— Win-Back's `owner_name` is a client CSV column for an individual owner,
not a corporate registry needing ForcedAction's own entity-detection
machinery.

`winback_rows` carries the enrichment state (`email_status`,
`email_previous`, `phone_verified`, `requires_enrichment_review`,
`enrichment_provider`, `enrichment_timestamp`, `enrichment_attempts` —
`migrations/apply_winback_enrichment.py`; no `TENANT_POLICIES` entry
needed, RLS is column-agnostic on an already-registered table).
`requires_enrichment_review = (no usable email) AND (no verified phone)` —
deliberately **not** `email_status != 'VERIFIED'`, which would recreate the
exact trap `contacts.email_status` is already in (nothing in this repo
ever writes `VERIFIED` except by hand in `scripts/e2e_approval_gate.py`, so
`compliance_gate._check_deterministic_columns` hard-`FAIL`s on every cold
contact today — a pre-existing gap this subtask deliberately does not
import into Win-Back, though it is the same missing enrichment step and
should be raised as a follow-up against Week 1's compliance gate).

`src/tasks/enrichment_verification.py` sweeps
self-heal → claim → enrich → scrub → log → post, `--client-id` never
defaulted (an unscoped sweep would silently no-op under RLS otherwise —
same posture as `work_orders`' own CLI). Self-heal runs first: a row whose
`enrichment_attempts` hits `OWNER_ENRICHMENT_MAX_ATTEMPTS` without ever
getting an answer is terminally marked
(`enrichment_timestamp`/`requires_enrichment_review=TRUE`) — without this,
`requires_enrichment_review`'s `NOT NULL DEFAULT FALSE` makes a
never-enriched row indistinguishable from a happily-enriched one, exactly
the trap `_flag_unscrubbed()`'s own docstring already warns about one
column over. Newly-discovered phones are DNC-scrubbed through
`winback_ingest.dnc_scrub_rows()` (a public wrapper around the existing
`_run_dnc_scrub`), preserving Subtask 3.1.1's "DNC scrub before any
sequence can arm" ordering for numbers that didn't exist at import time.
Posts the pre-pilot summary to `#blackink-qa` (its first *periodic*
producer — the channel already carries three fire-and-forget error alerts)
and exits non-zero if that post didn't land, so a missing
`BLACKINK_QA_SLACK_CHANNEL`/bot-not-in-channel can never be mistaken for a
passing gate.

`src/services/winback_sequencer.py`'s `evaluate_winback_touch_gate` gates
on `enrichment_timestamp IS NOT NULL` **before** checking
`requires_enrichment_review` — the same "must have run, not merely have
failed to object" reasoning as self-heal above. The `/arm` endpoint's SQL
(`src/api/winback_router.py`) carries the identical predicate, because
`arm_winback_run` inserts the `agent_work_orders` row and posts the Slack
approval card *before* any touch gate runs — a gate-only implementation
would still post an approval card for an un-enriched row.

**Subtask 3.1.1's ingest was also fixed here**: `winback_ingest.py`'s
`_REQUIRED_CSV_COLUMNS` no longer includes `phone`/`email` — that
five-column "minimum required" list traced only to the derived
`Week2_Tasks_Dev_Split_v1.md:236`, never to the client, and requiring phone
meant a client export with no phone column produced zero sequenceable
rows, defeating this subtask's own purpose. An owner with `phone=None` is
not DNC-scrubbed (nothing to scrub, not suppressed) — distinct from a
phone value present but ungradeable (`"n/a"`), which stays fail-closed
exactly as before.

Enrichment does **not** cover Speed-to-Lead's inbound leads
(`src/services/inbound_lead_orchestrator.py`, Task 4.2.1) — those are
inbound-initiated (a webhook/email the owner sent *to us*), so by
construction almost every row already carries a real contact method.
`owner_enrichment.EnrichmentInput`/`apply_result` are the contract whoever
picks up that rare no-contact case would call, with
`signal_source="SPEED_TO_LEAD"`. Same for the deed engine's homestead-drop
and same-owner-match signals (client comments:28-29, deferred this sprint
per `Week2_Tasks_Dev_Split_v1.md:11`) —
`owner_enrichment.enrich_homestead_drop_signals()` is a documented no-op
hook, called from the sweep so it is on the real code path, not orphaned.

## Tooling Rules

- **Language/runtime**: Python 3.11+.
- **Web framework**: FastAPI.
- **ORM**: SQLAlchemy 2.0 declarative style (`src/core/models.py` is the
  schema source of truth). Runtime queries use `sqlalchemy.text()` with
  named binds — never the ORM query API — except `session.add()` for
  single-row writes.
- **Migrations**: idempotent `migrations/apply_<name>.py` scripts
  (`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`), no Alembic.
- **Settings**: Pydantic v2 + pydantic-settings, `config/settings.py`,
  never read `os.environ` directly elsewhere.
- **Fuzzy matching**: rapidfuzz.
- **Redis**: tenant-scoped code MUST go through `src/core/tenant_redis.py`
  (`client_id` is a required positional arg), never the raw primitives in
  `src/core/redis_client.py` directly.
- **Testing**: pytest. Most tests run without a live DB (FakeSession
  pattern); `tests/test_tenant_isolation.py` requires a real Postgres with
  migrations applied.

## Self-Maintenance

Update after: new `src/` package, new `config/*.py`, new pytest marker,
new external integration, schema change in `models.py`, new scheduled task,
a new tenant-bearing table (must also update `config/tenant_policies.py`).
