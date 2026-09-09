"""Idempotency log for Ghost Shopper form submissions.

fill_and_submit writes a PENDING row before firing Playwright, then updates
to SUBMITTED on success. On retry after a checkpointer failure, the node
finds SUBMITTED and skips the form POST — preventing double-submission.

The DB write uses a separate connection that commits independently of
LangGraph's checkpointer session, so a checkpointer failure cannot roll
back the idempotency record.

Not tenant-bearing — same pattern as ghost_shopper_replies.
No RLS. blackink_system only.

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_ghost_form_submissions.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS ghost_form_submissions (
        id             BIGSERIAL    PRIMARY KEY,
        work_order_id  TEXT         NOT NULL,
        company_id     TEXT         NOT NULL,
        form_url       TEXT         NOT NULL,
        status         TEXT         NOT NULL DEFAULT 'PENDING',
        submitted_at   BIGINT,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT ghost_form_submissions_uq UNIQUE (work_order_id, form_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_gfs_work_order ON ghost_form_submissions (work_order_id)",
    "GRANT SELECT, INSERT, UPDATE ON ghost_form_submissions TO blackink_system",
    "GRANT USAGE ON SEQUENCE ghost_form_submissions_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()

    print("apply_ghost_form_submissions: OK")
    print("  ghost_form_submissions: created (work_order_id, company_id, form_url,")
    print("                                   status, submitted_at)")
    print("  unique constraint: (work_order_id, form_url)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
