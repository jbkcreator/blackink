"""
Provision the inbound_messages table — the durability layer for forwarded
owner-inquiry emails received by the Respond agent.

One row per inbound delivery. Idempotency key is SHA-256(destination_address +
original_message_id), preventing double-processing on SMTP re-delivery.
The classifier worker updates status/intent/classified_at on completion.

Tenant-scoped by client_id — must also be registered in TENANT_POLICIES and
included in apply_rls_policies.py (run this before apply_rls_policies.py).

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
        id                  BIGSERIAL       PRIMARY KEY,
        client_id           VARCHAR(40)     NOT NULL REFERENCES clients(client_id),
        idempotency_key     VARCHAR(128)    NOT NULL,
        destination_address VARCHAR(320)    NOT NULL,
        original_message_id VARCHAR(998),
        sender_email        VARCHAR(320)    NOT NULL,
        sender_name         VARCHAR(256),
        subject             VARCHAR(998),
        body_text           TEXT,
        body_html           TEXT,
        received_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        status              VARCHAR(30)     NOT NULL DEFAULT 'PENDING',
        intent              VARCHAR(30),
        intent_confidence   NUMERIC(4,3),
        classified_at       TIMESTAMPTZ,
        classification_meta JSONB           NOT NULL DEFAULT '{}'::jsonb,
        CONSTRAINT uq_inbound_messages_idempotency UNIQUE (idempotency_key),
        CONSTRAINT ck_inbound_messages_status
            CHECK (status IN ('PENDING', 'PROCESSING', 'CLASSIFIED', 'FAILED', 'SUPPRESSED', 'ESCALATED', 'DEFERRED', 'ROUTED'))
    )
    """,
    # Client + time index for the context-card query and
    # the SLA monitor — most queries filter by client_id first.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_client_time ON inbound_messages (client_id, received_at DESC)",
    # Status partial index: only PENDING rows, used by the stale-sweep.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_pending ON inbound_messages (id) WHERE status = 'PENDING'",
    # Per-sender index for dedup checks and context-card history.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_sender ON inbound_messages (client_id, sender_email)",
    "GRANT SELECT, INSERT, UPDATE ON inbound_messages TO blackink_app",
    "GRANT USAGE ON SEQUENCE inbound_messages_id_seq TO blackink_app",
    # Worker runs as system (BYPASSRLS) to update rows across any client.
    "GRANT SELECT, INSERT, UPDATE ON inbound_messages TO blackink_system",
    "GRANT USAGE ON SEQUENCE inbound_messages_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_inbound_messages: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
