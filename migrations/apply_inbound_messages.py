"""
Provision inbound_messages — stores forwarded prospect replies for the interim
reply bridge (Task 3.1.3).

Registered in TENANT_POLICIES (direct, client_id). Run after apply_clients.py
and apply_sequence_runs.py, and before apply_rls_policies.py.

Schema design (wayfinder tickets 20, 28):
  - message_id  — the inbound email's own RFC Message-ID; UNIQUE dedup key.
    Dedup also cross-checks against sequence_touch_dispatches.message_id to
    drop our own BCC echoes (handled at application layer, not DB constraint,
    because the cross-table check requires knowing both sets).
  - to_alias    — the {client_id}@inbound.getblackink.com alias the mail
    arrived on; used to derive client_id at ingest.
  - contact_id / run_id — nullable; null while unattributed. Two-tier
    attribution: In-Reply-To → sequence_touch_dispatches.message_id →
    run → contact; else sender email → contacts.email scoped to client.
  - attribution_status — 'attributed' or 'unattributed'. Never a guess.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_inbound_messages.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS inbound_messages (
        id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
        message_id         TEXT         NOT NULL,
        client_id          VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
        contact_id         BIGINT,
        run_id             UUID,
        from_address       TEXT         NOT NULL,
        to_alias           TEXT         NOT NULL,
        in_reply_to        TEXT,
        subject            TEXT,
        raw_body           TEXT,
        attribution_status VARCHAR(20)  NOT NULL DEFAULT 'unattributed',
        received_at        TIMESTAMPTZ  NOT NULL,
        created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
        CONSTRAINT chk_inbound_attribution
            CHECK (attribution_status IN ('attributed', 'unattributed')),
        CONSTRAINT uq_inbound_message_id
            UNIQUE (message_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_client ON inbound_messages (client_id)",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_contact ON inbound_messages (contact_id) WHERE contact_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_run ON inbound_messages (run_id) WHERE run_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_received ON inbound_messages (received_at DESC)",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'inbound_messages' ORDER BY ordinal_position"
            )
        ).fetchall()
    print("apply_inbound_messages: done —", [c.column_name for c in cols])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
