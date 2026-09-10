"""Partial unique index backing per-dispatch dedup of email engagement
events (S-8/S-11 code-review fix). Additive only — one index on the
existing `events` table, no schema/column change, no new table.

Without this, the application-level pre-check in
src/services/events.py::already_logged_for_dispatch() (a plain SELECT
before INSERT) is the ONLY defense against a duplicate email_opened/
email_clicked/email_replied row for the same dispatch — a database-level
race (two concurrent hits both passing the check before either commits)
could otherwise produce two rows, inflating daily_digest.py's
open/click/reply rate percentages above what "percent of sent emails
engaged with" should mean.

Rollback: additive only (one index, no data change) — safe to
`DROP INDEX uq_events_dispatch_dedup;` if ever needed.

    PYTHONPATH=. python migrations/apply_events_dispatch_dedup_index.py
"""

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    # Partial: only the three dispatch-scoped event types need this
    # constraint — every other event_type is unaffected and free to repeat
    # (e.g. inbound_reply_received, which is legitimately one row per
    # inbound message, not per dispatch).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_events_dispatch_dedup
        ON events (client_id, event_type, (payload->>'dispatch_id'))
        WHERE event_type IN ('email_opened', 'email_clicked', 'email_replied')
    """,
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_events_dispatch_dedup_index: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
