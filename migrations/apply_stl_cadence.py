"""Task 4.2.2 — Six-Attempt Speed-to-Lead Cadence schema.

Additive/idempotent: safe to run against a DB that already has inbound_messages
(from apply_inbound_messages_lead_fields.py) and agent_work_orders (from
apply_agent_work_orders.py). Run AFTER apply_inbound_messages_lead_fields.py
and BEFORE apply_rls_policies.py.

Changes:
  1. Three stop-latch columns on inbound_messages (cadence_state,
     cadence_stopped_at, cadence_stop_reason).
  2. New stl_cadence_dispatches table — at-most-once send guard per touch
     (mirrors winback_touch_dispatches: UNIQUE(message_id, touch_step)).
  3. Reserved clients.respond_early_phase column (always TRUE this year;
     wired into auto-send path in a future build when clients graduate).
"""

from __future__ import annotations

import logging
import os

import psycopg2

logger = logging.getLogger(__name__)

_STEPS = [
    # ── 1. Stop-latch on inbound_messages ────────────────────────────────────
    # Distinct from status (RECEIVED/SENDING/RESPONDED): cadence lifecycle is
    # tracked separately so Dev 2's triage status path is never entangled with
    # the cadence state machine.
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS cadence_state VARCHAR(20)",
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS cadence_stopped_at TIMESTAMPTZ",
    # Stop reason mirrors winback_rows.stop_reason: REPLY | BOOKED | OPT_OUT
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS cadence_stop_reason VARCHAR(20)",

    # Partial index: arm-check sweep scans only rows that have reached ARMED
    # and have not yet stopped — these are the only rows that need periodic
    # gate re-checks.
    "CREATE INDEX IF NOT EXISTS ix_inbound_messages_cadence_armed "
    "ON inbound_messages (cadence_state) WHERE cadence_state = 'ARMED'",

    # ── 2. stl_cadence_dispatches — at-most-once touch guard ─────────────────
    # Keyed on (message_id, touch_step) — same idea as sequence_touch_dispatches
    # keyed on (run_id, touch_step) and winback_touch_dispatches keyed on
    # (winback_row_id, touch_step).  Uses UUID PK (gen_random_uuid) matching
    # winback_touch_dispatches' own convention.
    # client_id is included as a direct column for RLS policy and
    # for the mailbox 24-h cap subquery in mailbox_dispatcher.
    """
    CREATE TABLE IF NOT EXISTS stl_cadence_dispatches (
        dispatch_id  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        client_id    VARCHAR(40) NOT NULL,
        message_id   BIGINT      NOT NULL,
        touch_step   SMALLINT    NOT NULL,
        status       VARCHAR(20) NOT NULL DEFAULT 'SENDING'
                     CHECK (status IN ('SENDING', 'SENT', 'FAILED', 'SENT_UNCONFIRMED')),
        mailbox_id   BIGINT,
        sent_at      TIMESTAMPTZ,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_stl_dispatch_one_per_touch UNIQUE (message_id, touch_step)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_stl_cadence_dispatches_client "
    "ON stl_cadence_dispatches (client_id)",
    "CREATE INDEX IF NOT EXISTS ix_stl_cadence_dispatches_message "
    "ON stl_cadence_dispatches (message_id)",

    # ── 3. Reserved per-client early-phase flag ───────────────────────────────
    # v2 blueprint mandates human-approval-per-send in early client phase.
    # Default TRUE (every client is early phase at pilot). The auto-send path
    # (autonomy_band BAND_0 for cadence touches) is NOT wired this year; this
    # column is the seam for a future build.
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS respond_early_phase BOOLEAN NOT NULL DEFAULT TRUE",
]


def run() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set")

    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        for step in _STEPS:
            logger.info("[apply_stl_cadence] %s", step[:80])
            cur.execute(step)
        conn.commit()
        logger.info("[apply_stl_cadence] done")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
