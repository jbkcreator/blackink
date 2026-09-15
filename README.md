# Blackink

Blackink is a multi-tenant B2B growth platform for property-management firms. It ingests and enriches prospect data, runs compliant outreach workflows, routes inbound replies, coordinates booking and follow-up operations, and records billing and settlement activity.

The backend is a Python/FastAPI service backed by PostgreSQL and Redis. It integrates with Slack, email providers, calendar providers, Stripe, Google Sheets, and selected enrichment/deliverability vendors.

## What it does

- Enforces tenant isolation for every client-facing workflow.
- Ingests prospect data through a restricted Akrash staging path, then promotes eligible prospects into tenant-scoped records.
- Runs outreach through the Cora, Ink, Relay, Hunter, Respond, and Vera agents.
- Applies compliance, suppression, approval, deliverability, and human-in-the-loop gates before dispatching messages.
- Handles inbound leads and email replies, booking webhooks, calendar OAuth, no-show recovery, reminders, and meeting outcomes.
- Provides Owner Visibility Score (OVS), self-serve audit, Win-Back, client-wins, and operational metrics capabilities.
- Supports payment authorization, settlement evidence, billing credits, and Stripe webhooks.

## Architecture at a glance

```text
External sources/webhooks
        |
        v
FastAPI API  ---> PostgreSQL (source of record)
   |                      |
   |                      +-- Row-Level Security for tenant data
   v
Redis queues/streams
   |
   +--> Cora / Ink / Relay / Hunter / Respond workers
   +--> scheduled sweeps and Slack approval flows
```

The core security invariant is strict `client_id` isolation. Tenant-bearing tables are registered in `config/tenant_policies.py`, protected by forced PostgreSQL RLS, and scoped in the application through `session_scope(client_id=...)`. Do not add a tenant-bearing table without adding its policy and migration coverage.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/api/` | FastAPI entrypoint and HTTP/webhook routers. |
| `src/agents/` | Cora, Ink, Relay, Hunter, Respond, and Vera agent implementations. |
| `src/services/` | Domain services: campaigns, compliance, bookings, billing, settlement, OVS, Slack, and integrations. |
| `src/tasks/` | Sweep and scheduled-worker entrypoints. |
| `src/core/` | Database, Redis, models, encryption, and tenant-scoping primitives. |
| `config/` | Application settings, tenant policy registry, Slack channels, Redis keys, and prompt variants. |
| `migrations/` | Idempotent, ordered PostgreSQL migration scripts. |
| `scripts/` | Operational, E2E, demo, diagnostic, and migration helpers. |
| `tests/` | Pytest suite, including tenant-isolation and integration coverage. |
| `deploy/systemd/` | Linux systemd unit examples for the API and Cora worker. |

## Prerequisites

- Python 3.11
- PostgreSQL 16 (or a compatible PostgreSQL server)
- Redis
- A copy of `.env.example` configured as `.env`

Optional integrations need their corresponding credentials only when their flows are enabled: Slack, Instantly/SMTP/Mailgun, Google or Microsoft calendar OAuth, Stripe, Anthropic, Tracerfy, Google Places, Google Sheets, IMAP, Oxylabs, and Sendspark.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env               # Windows PowerShell: Copy-Item .env.example .env
```

Set at least the database and Redis connection values in `.env`. For application traffic, `DATABASE_URL_APP` must use the RLS-subject `blackink_app` role; batch-only work uses `DATABASE_URL_SYSTEM`. See `.env.example` and `config/settings.py` for the full settings reference.

Apply the schema before starting the service:

```bash
bash scripts/run_migrations.sh
```

The migration helper runs the required scripts in dependency order and is safe to rerun. It is written for a POSIX shell; on Windows, run it through WSL/Git Bash or execute the migration modules in the documented order in `CLAUDE.md`.

Start the API:

```bash
uvicorn src.api.main:app --reload --port 8000
```

Verify it is running:

```bash
curl http://localhost:8000/healthz
```

The API exposes interactive OpenAPI documentation at `http://localhost:8000/docs` when running locally.

## Local database with Docker

`docker-compose.yml` includes a disposable development PostgreSQL service on port `5433` to avoid colliding with a local PostgreSQL instance:

```bash
docker compose up -d postgres-test
```

Point the local database variables in `.env` at that instance, run migrations, then start the API. The compose file also contains convenience definitions for the API and a few local sweep processes; it is not the production deployment method.

## Workers and scheduled work

`src/api/main.py` starts the Slack Socket Mode task and several in-process sweeps, including calendar sync, confirmations, reminders, no-show flows, settlement, billing, speed-to-lead, Respond SLA, sequence/work-order execution, Vera health, and reactivation. Run a single API worker/instance unless those duties are deliberately separated; duplicate instances can create duplicate sweeps and Slack connections.

Other jobs can be run independently, for example:

```bash
python -m src.agents.cora.worker
python -m src.agents.relay.worker
python -m src.agents.respond.worker
python -m src.tasks.promotion_sweep
python -m src.tasks.deliverability_sentinel
python -m src.tasks.assessor_sync
```

For server scheduling, review and install the appropriate entries from `scripts/crontab.txt`. Systemd service examples are available in `deploy/systemd/`.

## Testing

```bash
pytest tests/
```

Most tests run without a database. Tests that exercise database/RLS behavior require a live PostgreSQL instance with migrations applied; for example:

```bash
pytest tests/test_tenant_isolation.py
```

## Deployment with systemd

Production deployment is managed with systemd, using the unit examples in `deploy/systemd/`:

- `blackink-api.service` runs the FastAPI/Uvicorn process.
- `blackink-cora.service` runs the Cora draft-generation worker.

Both units expect the application at `/root/blackink`, load `/root/blackink/.env`, and run with `PYTHONPATH=/root/blackink`. Adapt those paths and the virtual-environment executable to the server’s actual layout before installing them.

The API unit deliberately runs a single Uvicorn process. Its lifespan starts the Slack Socket Mode task and in-process sweeps, so adding Uvicorn workers or duplicate API units would duplicate Slack connections and scheduled work. Run independent workers and cron jobs only from their documented service or scheduler entrypoints.

After updating a unit file, reload systemd and manage the services with the usual commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now blackink-api
sudo systemctl status blackink-api
```

Keep `.env` readable only by the service account and never commit it. Provide database DSNs and integration credentials through that environment file or the host’s secret-management mechanism.

## Operational safety

- Keep `.env`, service-account keys, local data, and generated logs out of version control.
- Preserve fail-closed settings such as outbound email, inbound webhook secrets, IMAP, and approved-host lists until their dependencies are genuinely configured.
- Run `apply_rls_policies.py` as part of every initial schema setup; the migration runner places it near the end of its sequence.
- Avoid using the system/BYPASSRLS database role from request-handling code.
- Review `CLAUDE.md` for the canonical migration sequence, deployment constraints, architecture rationale, and detailed operating guidance.

## Further documentation

See `CLAUDE.md` for the canonical migration sequence, architecture rationale, and detailed operating guidance. This README intentionally stays focused on onboarding, runtime architecture, and safe day-to-day operation.
