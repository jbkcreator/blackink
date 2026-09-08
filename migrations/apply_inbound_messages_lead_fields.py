"""
Extend inbound_messages with Task 4.2.1 Speed-to-Lead (Dev 4) columns.

Dev 2's reply-triage agent OWNS the base inbound_messages table (id PK,
idempotency_key, sender_email, destination_address, body_text/html, the
intent/* + card_* + claim_* triage columns). Dev 4's Speed-to-Lead ingest
reuses that same table and column names, adding only the fields it needs.

This migration is ADDITIVE and idempotent, and works whether or not Dev 2's
creating migration has run yet:
  - If the base table already exists (the server), CREATE TABLE IF NOT EXISTS
    no-ops and only the ADD COLUMN / CHECK-widening statements take effect.
  - On a fresh local DB where Dev 2's creator has not run, CREATE TABLE builds
    a base schema byte-compatible with the server's so Dev 2's code works too.

Dev 4 ↔ Dev 2 column mapping (adopting Dev 2's names):
  message_id     → id (BIGSERIAL)
  dedupe_key     → idempotency_key (namespaced "<client_id>:<key>")
  prospect_email → sender_email
  prospect_name  → sender_name
  cleaned_body   → body_text
Dev 4-only additions: channel, source_channel, send_at, lead_sla_due_at,
  ack_latency_seconds, property_address, sender_phone, utm.

Status lifecycles stay disjoint: Dev 2 owns PENDING→…→ROUTED; Dev 4 uses
RECEIVED→RESPONDED (both added to the CHECK, purely widening). Dev 2 filters
its worker with `WHERE channel IS NULL OR channel = 'EMAIL_REPLY'` (its own
rows have channel NULL); Dev 4 rows carry channel = 'EMAIL' | 'WEBHOOK'.

Idempotent. Run BEFORE apply_rls_policies.py.

    PYTHONPATH=. python migrations/apply_inbound_messages_lead_fields.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

# Base table — mirrors Dev 2's deployed schema exactly, so a fresh local DB
# ends up identical to the server. No-ops where the table already exists.
CREATE_BASE = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    id                    BIGSERIAL    PRIMARY KEY,
    client_id             VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
    idempotency_key       VARCHAR(255) NOT NULL,
    destination_address   VARCHAR(255) NOT NULL,
    original_message_id   VARCHAR(255),
    sender_email          VARCHAR(255) NOT NULL,
    sender_name           VARCHAR(255),
    subject               VARCHAR(500),
    body_text             TEXT,
    body_html             TEXT,
    received_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    intent                VARCHAR(30),
    intent_confidence     NUMERIC(4,3),
    classified_at         TIMESTAMPTZ,
    classification_meta   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    sla_due_at            TIMESTAMPTZ,
    claimed_at            TIMESTAMPTZ,
    claimed_by            VARCHAR(100),
    card_posted_at        TIMESTAMPTZ,
    card_ts               VARCHAR(50),
    card_channel_id       VARCHAR(50),
    escalation_level      SMALLINT     NOT NULL DEFAULT 0,
    requires_human_review BOOLEAN      NOT NULL DEFAULT FALSE
)
"""

DDL = [
    CREATE_BASE,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_messages_idempotency ON inbound_messages (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_client_time ON inbound_messages (client_id, received_at DESC)",
    # ── Dev 4 Speed-to-Lead columns ──────────────────────────────────────────
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS channel VARCHAR(20)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS source_channel VARCHAR(40)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS send_at TIMESTAMPTZ",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS lead_sla_due_at TIMESTAMPTZ",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS ack_latency_seconds DOUBLE PRECISION",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS property_address TEXT",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS sender_phone VARCHAR(40)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS utm JSONB",
    # Which mailbox sent the auto-response + when — so the mailbox picker can
    # count Speed-to-Lead sends against the per-mailbox rolling-24h cap.
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS mailbox_id BIGINT",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS responded_at TIMESTAMPTZ",
    # Widen the status CHECK to include Dev 4's RECEIVED/SENDING/RESPONDED
    # (additive — never removes a Dev 2 value). SENDING is the sweep's own
    # in-progress sentinel, distinct from Dev 2's DEFERRED (LATER intent).
    # Drop-and-recreate so re-running is safe.
    "ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS ck_inbound_messages_status",
    """
    ALTER TABLE inbound_messages ADD CONSTRAINT ck_inbound_messages_status CHECK (
        status IN (
            'PENDING','PROCESSING','CLASSIFIED','FAILED','SUPPRESSED','ESCALATED',
            'DEFERRED','ROUTED','REALLOCATED',
            'RECEIVED','SENDING','RESPONDED'
        )
    )
    """,
    # Dev 4 sweep claim index: due, unresponded leads.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_lead_due ON inbound_messages (send_at) WHERE status = 'RECEIVED'",
    # Capacity count: responses sent per mailbox in the rolling 24h window.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_mailbox_responded ON inbound_messages (mailbox_id, responded_at) WHERE mailbox_id IS NOT NULL",
    # Grants (idempotent) — needed on a fresh local DB; no-op re-grant on server.
    "GRANT SELECT, INSERT, UPDATE ON inbound_messages TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON inbound_messages TO blackink_system",
    "GRANT USAGE ON SEQUENCE inbound_messages_id_seq TO blackink_app",
    "GRANT USAGE ON SEQUENCE inbound_messages_id_seq TO blackink_system",
]


def main() -> None:
    with get_owner_db_context() as session:
        for stmt in DDL:
            session.execute(text(stmt))
        session.commit()
    print("apply_inbound_messages_lead_fields: done")


if __name__ == "__main__":
    main()
