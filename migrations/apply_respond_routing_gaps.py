"""
Routing-gap fixes for Subtask 2.1.1 completion.

Changes:
  inbound_messages:
    requires_human_review BOOLEAN NOT NULL DEFAULT FALSE
      — set TRUE when QUESTION confidence < 0.90

  sequence_runs:
    HALTED added to status check constraint
      — written by the worker when HOT_LEAD or COMPLAINT is routed

Idempotent: ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT pattern.

    PYTHONPATH=. python migrations/apply_respond_routing_gaps.py
"""
import sys
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # inbound_messages — human-review flag for QUESTION path
    "ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS requires_human_review BOOLEAN NOT NULL DEFAULT FALSE",

    # sequence_runs — widen status to include HALTED
    "ALTER TABLE sequence_runs DROP CONSTRAINT IF EXISTS chk_sequence_runs_status",
    """
    ALTER TABLE sequence_runs ADD CONSTRAINT chk_sequence_runs_status
        CHECK (status IN ('ACTIVE', 'HALTED', 'COMPLETED', 'CANCELLED'))
    """,
]

if __name__ == "__main__":
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_respond_routing_gaps: done")
