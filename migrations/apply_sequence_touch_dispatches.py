"""Provision sequence_touch_dispatches — the at-most-once email dispatch guard (Task 3.1.1).

Run after apply_sequence_runs.py and before apply_rls_policies.py.

The UNIQUE (run_id, touch_step) constraint is the core idempotency lock:
INSERT ... ON CONFLICT DO NOTHING is the claim mechanism. A row stuck in
SENDING for > 30 min is a stuck-job signal — alert #blackink-qa.

    PYTHONPATH=. python migrations/apply_sequence_touch_dispatches.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS sequence_touch_dispatches (
        dispatch_id   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        client_id     VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        run_id        UUID         NOT NULL REFERENCES sequence_runs(run_id),
        touch_step    SMALLINT     NOT NULL,
        status        VARCHAR(20)  NOT NULL DEFAULT 'SENDING',
        message_id    TEXT,
        mailbox_id    INTEGER,
        sent_at       TIMESTAMPTZ,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT chk_dispatch_status CHECK (status IN ('SENDING', 'SENT', 'FAILED')),
        CONSTRAINT uq_dispatch_one_per_touch UNIQUE (run_id, touch_step)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_std_client ON sequence_touch_dispatches (client_id)",
    "CREATE INDEX IF NOT EXISTS ix_std_run ON sequence_touch_dispatches (run_id)",
    "CREATE INDEX IF NOT EXISTS ix_std_status ON sequence_touch_dispatches (status, created_at)",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'sequence_touch_dispatches' ORDER BY ordinal_position"
            )
        ).fetchall()
    print("apply_sequence_touch_dispatches: done —", [c.column_name for c in cols])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
