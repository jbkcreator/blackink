"""
Provision the inbound_messages table — tenant-bearing, superset design
so Dev 4's Speed-to-Lead ingest (4.2.1) and Dev 2's reply-triage agent
share one table with zero colliding migrations.

All columns (Dev 4 ingest + Dev 2 triage) are included at CREATE TABLE
time as nullable where they're not needed by both. This supersedes
Dev 2's three incremental ALTER TABLE migrations
(apply_inbound_messages_sla.py, apply_respond_routing_gaps.py, etc.) —
those must NOT be run if this migration has already created the table.

Column ownership:
  Dev 4 (ingest):  channel, source_channel, raw_payload, cleaned_body,
                   received_at, send_at, lead_sla_due_at,
                   ack_latency_seconds, dedupe_key, utm
  Dev 2 (triage):  intent, intent_confidence, classified_at,
                   classification_meta, sla_due_at (triage rep-claim
                   deadline), card_*, claimed_*, escalation_level
  Shared:          message_id, client_id, contact_id, status,
                   requires_human_review

NOTE: sla_due_at is Dev 2's rep-claim SLA (set at classification time:
received_at + 15 min for HOT_LEAD/WHALE_OWNER, + 60 min otherwise).
Dev 4's 30-min lead-response SLA uses the separate lead_sla_due_at
column to avoid two sweeps fighting over one timestamp.

Dev 2 filter note: the classifier should add WHERE channel = 'EMAIL' to
exclude non-email inbound rows (form-fill webhooks, etc.) that the
classifier is not designed to classify.

Idempotent: CREATE TABLE IF NOT EXISTS.
Run BEFORE apply_rls_policies.py.

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
        -- ── Identity ──────────────────────────────────────────────────────
        message_id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
        client_id               VARCHAR(40)     NOT NULL REFERENCES clients(client_id),
        contact_id              BIGINT          REFERENCES contacts(contact_id),

        -- ── Dev 4 ingest columns ──────────────────────────────────────────
        channel                 VARCHAR(20)     NOT NULL
                                CHECK (channel IN ('EMAIL', 'WEBHOOK')),
        source_channel          VARCHAR(40)     NOT NULL,
        raw_payload             TEXT,
        cleaned_body            TEXT,
        received_at             TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        send_at                 TIMESTAMPTZ,
        lead_sla_due_at         TIMESTAMPTZ,
        ack_latency_seconds     DOUBLE PRECISION,
        dedupe_key              VARCHAR(255)    NOT NULL,
        utm                     JSONB,

        -- ── Shared / lifecycle ────────────────────────────────────────────
        status                  VARCHAR(20)     NOT NULL DEFAULT 'RECEIVED'
                                CHECK (status IN (
                                    'RECEIVED', 'RESPONDED', 'SUPPRESSED', 'DEFERRED',
                                    'PENDING', 'PROCESSING', 'ROUTED', 'ESCALATED',
                                    'REALLOCATED', 'FAILED'
                                )),
        requires_human_review   BOOLEAN         NOT NULL DEFAULT FALSE,
        created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

        -- ── Dev 2 triage columns (nullable until triage agent lands) ──────
        intent                  VARCHAR(30),
        intent_confidence       NUMERIC(4,3),
        classified_at           TIMESTAMPTZ,
        classification_meta     JSONB           NOT NULL DEFAULT '{}'::jsonb,
        sla_due_at              TIMESTAMPTZ,
        card_posted_at          TIMESTAMPTZ,
        card_ts                 VARCHAR(50),
        card_channel_id         VARCHAR(50),
        claimed_at              TIMESTAMPTZ,
        claimed_by              VARCHAR(100),
        escalation_level        SMALLINT        NOT NULL DEFAULT 0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_messages_dedupe_key ON inbound_messages (client_id, dedupe_key)",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_client_status ON inbound_messages (client_id, status, send_at)",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_received_at ON inbound_messages (received_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_intent ON inbound_messages (client_id, intent, classified_at)",
]


def main() -> None:
    with get_owner_db_context() as session:
        for stmt in DDL:
            session.execute(text(stmt))
        session.commit()
    print("apply_inbound_messages: done")


if __name__ == "__main__":
    main()
