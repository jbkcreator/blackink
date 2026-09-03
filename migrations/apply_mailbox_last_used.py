"""Add last_used_at to mailboxes — enables round-robin dispatch (Dev 4 / Week 0).

Idempotent: ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_mailbox_last_used.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE mailboxes ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ NULL;",
    "CREATE INDEX IF NOT EXISTS ix_mailboxes_last_used ON mailboxes (client_id, last_used_at ASC NULLS FIRST) WHERE quarantine_state = 'active' AND warmup_status = 'warmed';",
]


def main():
    with get_owner_db_context() as session:
        for stmt in DDL:
            session.execute(text(stmt))
        session.commit()
        print("apply_mailbox_last_used: done")


if __name__ == "__main__":
    main()
