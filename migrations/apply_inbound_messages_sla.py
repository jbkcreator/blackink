"""
Add SLA and context-card tracking columns to inbound_messages (Subtask 2.1.2).

New columns:
  sla_due_at       — absolute deadline written at classification time
  claimed_at       — timestamp when a rep clicked Claim on the context card
  claimed_by       — Slack user ID of the rep who claimed
  card_posted_at   — UTC timestamp when the Slack card was posted (24h expiry anchor)
  card_ts          — Slack message ts (for in-place card updates)
  card_channel_id  — Slack channel ID the card was posted to
  escalation_level — 0=not yet escalated, 1=first ping, 2=command alert, 3=reallocated

Also adds REALLOCATED to the status check constraint (tier-3 escalation terminal).

Idempotent: ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS.

    PYTHONPATH=. python migrations/apply_inbound_messages_sla.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS sla_due_at       TIMESTAMPTZ",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS claimed_at        TIMESTAMPTZ",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS claimed_by        VARCHAR(100)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS card_posted_at    TIMESTAMPTZ",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS card_ts           VARCHAR(50)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS card_channel_id   VARCHAR(50)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS escalation_level  SMALLINT NOT NULL DEFAULT 0",
    # Widen the status check constraint to include REALLOCATED (tier-3 escalation).
    "ALTER TABLE inbound_messages DROP CONSTRAINT IF EXISTS ck_inbound_messages_status",
    """
    ALTER TABLE inbound_messages ADD CONSTRAINT ck_inbound_messages_status
        CHECK (status IN (
            'PENDING','PROCESSING','CLASSIFIED','FAILED',
            'SUPPRESSED','ESCALATED','DEFERRED','ROUTED','REALLOCATED'
        ))
    """,
    # Partial index: only ROUTED rows that haven't been claimed yet — the SLA sweep's core predicate.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_sla_unclaimed ON inbound_messages (received_at, sla_due_at) WHERE status = 'ROUTED' AND claimed_at IS NULL",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_inbound_messages_sla: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
