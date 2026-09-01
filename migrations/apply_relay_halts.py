"""Provision the relay_halts table — persistent halt state for Relay.

relay_halts is NOT a tenant-bearing table — it is control-plane state for the
pipeline orchestrator, scoped to GLOBAL / CLIENT / CAMPAIGN levels, not to a
specific client_id in the RLS sense. Do NOT add it to config/tenant_policies.py.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

Run order: after apply_clients.py (CLIENT-scope rows reference client_id by
string value, but there is no FK — halt records must survive client deletes).

    PYTHONPATH=. python migrations/apply_relay_halts.py
"""

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS relay_halts (
        id          BIGSERIAL       PRIMARY KEY,
        scope       VARCHAR(20)     NOT NULL
                        CHECK (scope IN ('GLOBAL', 'CLIENT', 'CAMPAIGN')),
        scope_id    VARCHAR(100),
        reason      TEXT,
        issued_by   VARCHAR(200)    NOT NULL,
        issued_at   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        resumed_at  TIMESTAMPTZ,
        resumed_by  VARCHAR(200),
        is_active   BOOLEAN         NOT NULL DEFAULT TRUE
    )
    """,
    # Partial unique index — exactly one active halt per (scope, scope_id) pair.
    # scope_id is NULL for GLOBAL; COALESCE normalises that for the uniqueness check.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_relay_halts_active_scope
        ON relay_halts (scope, COALESCE(scope_id, ''))
        WHERE is_active IS TRUE
    """,
    "CREATE INDEX IF NOT EXISTS ix_relay_halts_active ON relay_halts (is_active) WHERE is_active IS TRUE",
    # Both roles can read halt state; both can issue and resume halts.
    # The blackink_app role is used by Slack command handlers (API layer).
    # The blackink_system role is used by workers and the startup sync.
    "GRANT SELECT, INSERT, UPDATE ON relay_halts TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON relay_halts TO blackink_system",
    "GRANT USAGE ON SEQUENCE relay_halts_id_seq TO blackink_app, blackink_system",
]


def main() -> int:
    with get_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_relay_halts: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
