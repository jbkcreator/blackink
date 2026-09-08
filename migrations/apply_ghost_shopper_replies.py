"""Audit log for Ghost Shopper inbound replies (and timeouts).

When the IMAP listener receives a reply from a PM firm that Ghost Shopper
submitted a form to, it records the event here before publishing the resume
signal to ink:resume_signals. Timeout sweeps also write a row (timed_out=True)
so every submission has a terminal record.

Not tenant-bearing — the PM firm being audited is an unallocated prospect
(companies.owning_client_id IS NULL), so no client_id column. Same pattern
as relay_halts and sms_dispatch_log.

No RLS. Accessible by blackink_system (BYPASSRLS) only — the IMAP listener
uses get_system_db_context().

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS throughout.

    PYTHONPATH=. python migrations/apply_ghost_shopper_replies.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS ghost_shopper_replies (
        id             BIGSERIAL    PRIMARY KEY,
        work_order_id  TEXT         NOT NULL,
        company_id     TEXT         NOT NULL,
        sender_domain  TEXT,
        received_at    TIMESTAMPTZ  NOT NULL,
        latency_sec    INTEGER,
        loss_est       INTEGER,
        timed_out      BOOLEAN      NOT NULL DEFAULT FALSE,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_gsr_work_order ON ghost_shopper_replies (work_order_id)",
    "CREATE INDEX IF NOT EXISTS ix_gsr_company    ON ghost_shopper_replies (company_id)",
    "GRANT SELECT, INSERT ON ghost_shopper_replies TO blackink_system",
    "GRANT USAGE ON SEQUENCE ghost_shopper_replies_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()

    print("apply_ghost_shopper_replies: OK")
    print("  ghost_shopper_replies: created (work_order_id, company_id, sender_domain,")
    print("                                  received_at, latency_sec, loss_est, timed_out)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
