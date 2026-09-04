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
PYTHONPATH=. python migrations/apply_area_code_timezones.py
PYTHONPATH=. python migrations/apply_clients.py
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
PYTHONPATH=. python migrations/apply_mailbox_smtp_credentials.py
PYTHONPATH=. python migrations/apply_owner_contacts.py
PYTHONPATH=. python migrations/apply_calendar_connections.py
PYTHONPATH=. python migrations/apply_bookings.py
PYTHONPATH=. python migrations/apply_booking_reminder_jobs.py      # Subtask 3.2.2 — Show-Rate Reminder Cascade
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
python -m src.tasks.sequence_sweep              # Dev 3 — posts due email-touch approval cards to Slack
python -m src.services.work_orders --sweep --client-id <id>  # Dev 3 — executes APPROVED touch dispatches

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
- `AKRASH_INGEST_JWT_SECRET`, `DNC_VENDOR_API_KEY`, etc. — the existing
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
