"""
Provision inbound_messages — shared table for the Reply Triage Agent (2.1.1)
and the Speed-to-Lead ingest bridge (4.2.1).

TRIAGE side (2.1.1):
  One row per inbound delivery. Idempotency key is
  SHA-256(destination_address + original_message_id), preventing
  double-processing on SMTP re-delivery. The classifier worker updates
  status/intent/classified_at on completion.

INGEST side (4.2.1):
  Two-tier attribution: In-Reply-To → sequence_touch_dispatches.message_id
  → run → contact; else sender_email → contacts.email scoped to client.
  contact_id / run_id are null while unattributed; attribution_status
  tracks whether attribution has been resolved.

Tenant-scoped by client_id (direct mode). Must be registered in
TENANT_POLICIES and run before apply_rls_policies.py.

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
        -- Primary key: BIGSERIAL — triage worker types db_id as int throughout.
        id                  BIGSERIAL       PRIMARY KEY,

        -- Tenant scope
        client_id           VARCHAR(40)     NOT NULL REFERENCES clients(client_id),

        -- Dedup — SHA-256(destination_address + original_message_id).
        -- Prevents double-processing on SMTP re-delivery.
        idempotency_key     VARCHAR(128)    NOT NULL,

        -- Envelope fields
        destination_address VARCHAR(320)    NOT NULL,  -- forwarding alias the mail arrived on
        original_message_id VARCHAR(998),              -- RFC Message-ID from the email headers
        in_reply_to         TEXT,                      -- In-Reply-To header (4.2.1 threading)
        sender_email        VARCHAR(320)    NOT NULL,
        sender_name         VARCHAR(256),
        subject             VARCHAR(998),
        body_text           TEXT,
        body_html           TEXT,
        received_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

        -- Triage workflow (2.1.1 / 2.1.2)
        status              VARCHAR(30)     NOT NULL DEFAULT 'PENDING',
        intent              VARCHAR(30),
        intent_confidence   NUMERIC(4,3),
        classified_at       TIMESTAMPTZ,
        classification_meta JSONB           NOT NULL DEFAULT '{}'::jsonb,

        -- Attribution (4.2.1)
        contact_id          BIGINT,
        run_id              UUID,
        attribution_status  VARCHAR(20)     NOT NULL DEFAULT 'unattributed',

        CONSTRAINT uq_inbound_messages_idempotency
            UNIQUE (idempotency_key),
        CONSTRAINT ck_inbound_messages_status
            CHECK (status IN (
                'PENDING', 'PROCESSING', 'ROUTED', 'SUPPRESSED',
                'ESCALATED', 'DEFERRED', 'REALLOCATED', 'FAILED'
            )),
        CONSTRAINT ck_inbound_attribution_status
            CHECK (attribution_status IN ('attributed', 'unattributed'))
    )
    """,
    # Client + time — most queries filter by client_id first.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_client_time ON inbound_messages (client_id, received_at DESC)",
    # Pending sweep — stale-message scanner.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_pending ON inbound_messages (id) WHERE status = 'PENDING'",
    # Per-sender — dedup checks and context-card engagement history.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_sender ON inbound_messages (client_id, sender_email)",
    # Attribution (4.2.1) — contact and run lookups.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_contact ON inbound_messages (contact_id) WHERE contact_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_run ON inbound_messages (run_id) WHERE run_id IS NOT NULL",
    # Grants
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
