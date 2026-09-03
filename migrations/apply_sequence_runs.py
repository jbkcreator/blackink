"""
Provision sequence_runs — the per-contact sequence lifecycle table (Task 3.1.1).

Run after apply_clients.py (FK target) and before apply_rls_policies.py.
Add to CLAUDE.md's ordered migration list after apply_agent_work_orders.py.

The partial unique index `uq_sequence_runs_one_active` is deliberately
cross-client (no client_id filter): a contact can only be in ONE active
sequence globally, regardless of which client enrolled them. This is the
active-sequence lock from the 3.1.1 design — enforced at the database level
so no application bug can bypass it. RLS still scopes reads/writes by
client_id; the index just adds the cross-client uniqueness on top.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_sequence_runs.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS sequence_runs (
        run_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        client_id    VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        contact_id   BIGINT       NOT NULL,
        status       VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
        enrolled_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        cooling_until TIMESTAMPTZ,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT chk_sequence_runs_status
            CHECK (status IN ('ACTIVE', 'COMPLETED', 'CANCELLED'))
    )
    """,
    # cooling_until: 30-day post-sequence cooling window, written when the final
    # touch dispatches (wayfinder ticket 07 — cooling lives on the run). ADD
    # COLUMN IF NOT EXISTS so this is idempotent on an already-created table.
    "ALTER TABLE sequence_runs ADD COLUMN IF NOT EXISTS cooling_until TIMESTAMPTZ",
    # Cross-client uniqueness: one ACTIVE run per contact, globally.
    # Deliberately not filtered by client_id — see module docstring.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_sequence_runs_one_active
        ON sequence_runs (contact_id)
        WHERE status = 'ACTIVE'
    """,
    "CREATE INDEX IF NOT EXISTS ix_sequence_runs_client ON sequence_runs (client_id)",
    "CREATE INDEX IF NOT EXISTS ix_sequence_runs_status ON sequence_runs (status, enrolled_at)",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'sequence_runs' ORDER BY ordinal_position"
            )
        ).fetchall()
    print("apply_sequence_runs: done —", [c.column_name for c in cols])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
