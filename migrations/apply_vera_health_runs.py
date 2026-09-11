"""Provision vera_health_runs — persisted Vera health-check results (S-1).

vera_health_runs is NOT a tenant-bearing table — every check it records is
platform-wide (pm_feed counts across all clients, pipeline counts all
staging rows regardless of owner, campaign_feed is one Instantly account for
the whole platform). Same class as relay_halts, which states this identical
reasoning in its own migration docstring. Do NOT add it to
config/tenant_policies.py.

Why a table at all, instead of calling run_health_checks() inline from every
settlement/billing sweep: (1) check_campaign_feed makes a live Instantly HTTP
call — with 7 sweep functions on 60s/300s/3600s ticks, calling it inline from
each would hammer a third party and make every sweep's latency depend on it;
(2) a health run must be inspectable by an operator after the fact, not just
implied by whether sweeps ran; (3) "no health run has ever completed" and "the
last health run is stale" are both real halt conditions (see
src/agents/vera/health_gate.py's module docstring) and can only be
represented if runs are recorded, not just computed on demand.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

Run order: no dependency on any other table — placed alongside
apply_relay_halts.py (after apply_clients.py, before apply_companies.py) as
another early, non-tenant, control-plane table.

Rollback: additive-only (one new table, no ALTERs to any existing table,
no other table has a foreign key to this one) — safe to revert with a plain
`DROP TABLE vera_health_runs;` against the owner role if this migration
needs to be undone.

    PYTHONPATH=. python migrations/apply_vera_health_runs.py
"""

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS vera_health_runs (
        id                  BIGSERIAL       PRIMARY KEY,
        ran_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        overall             VARCHAR(20)     NOT NULL
                                CHECK (overall IN ('OK', 'HALT')),
        pm_feed_state       VARCHAR(20)     NOT NULL
                                CHECK (pm_feed_state IN ('VALUE', 'UNKNOWN', 'ABSTAIN')),
        pm_feed_detail      TEXT            NOT NULL,
        campaign_feed_state VARCHAR(20)     NOT NULL
                                CHECK (campaign_feed_state IN ('VALUE', 'UNKNOWN', 'ABSTAIN')),
        campaign_feed_detail TEXT           NOT NULL,
        pipeline_state      VARCHAR(20)     NOT NULL
                                CHECK (pipeline_state IN ('VALUE', 'UNKNOWN', 'ABSTAIN')),
        pipeline_detail     TEXT            NOT NULL
    )
    """,
    # The gate only ever needs the single latest row — this index makes that
    # lookup O(log n) instead of a full-table ORDER BY ... LIMIT 1 scan as
    # the table grows (one row every 5 minutes, indefinitely).
    "CREATE INDEX IF NOT EXISTS ix_vera_health_runs_ran_at ON vera_health_runs (ran_at DESC)",
    # blackink_system writes runs (the scheduled health sweep); blackink_app
    # reads the latest row (the gate is called from inside settlement/billing
    # sweeps, which today run under blackink_system too, but the app role is
    # granted SELECT for parity with relay_halts and in case a future
    # app-layer surface needs to display the latest health state).
    "GRANT SELECT, INSERT ON vera_health_runs TO blackink_app",
    "GRANT SELECT, INSERT ON vera_health_runs TO blackink_system",
    "GRANT USAGE ON SEQUENCE vera_health_runs_id_seq TO blackink_app, blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_vera_health_runs: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
