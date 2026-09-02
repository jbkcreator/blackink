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
PYTHONPATH=. python migrations/apply_clients.py
PYTHONPATH=. python migrations/apply_companies.py
PYTHONPATH=. python migrations/apply_contacts.py
PYTHONPATH=. python migrations/apply_pm_profiles.py
PYTHONPATH=. python migrations/apply_owner_entities.py
PYTHONPATH=. python migrations/apply_raw_prospect_pipeline.py
PYTHONPATH=. python migrations/apply_events.py
PYTHONPATH=. python migrations/apply_compliance_gate_audit.py
PYTHONPATH=. python migrations/apply_sending_domains.py
PYTHONPATH=. python migrations/apply_rls_policies.py   # run LAST
PYTHONPATH=. python migrations/apply_akrash_grant.py    # run after RLS

# Background jobs
python -m src.tasks.promotion_sweep
python -m src.tasks.county_allocation_reassessment
python -m src.tasks.deliverability_sentinel

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
