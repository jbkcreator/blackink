"""Fix the mailbox rolling-24h cap query's index coverage (post-review finding
on Group D defect D-1).

D-1's fix to src/services/mailbox_dispatcher.py's cap subquery — widening the
status filter to `IN ('RESPONDED', 'SENT_UNCONFIRMED')` and wrapping the
timestamp in `COALESCE(responded_at, received_at)` (SENT_UNCONFIRMED rows
never get responded_at set) — made the existing
ix_inbound_messages_mailbox_responded index (mailbox_id, responded_at)
unusable for this query: a plain btree index on responded_at cannot satisfy
a filter on COALESCE(responded_at, received_at) (confirmed via EXPLAIN
against the live DB: Seq Scan, index not considered). That index has no
other caller anywhere in the codebase (grepped — its only consumer was this
exact query), so it is now dead weight, not a "keep both" situation.

get_active_mailbox_for_client() is the mailbox-picker's hot path, called on
every outbound send attempt (Speed-to-Lead auto-response, sequence touches,
win-back touches, STL cadence). At current data volume (tens of rows) a
sequential scan is free; at the sprint's target volume (hundreds/thousands
of Speed-to-Lead leads per day across dozens of mailboxes) this becomes a
full-table scan on every single send — the exact "claim query against a
growing table needs a covering index" case.

Fix: drop the now-unusable index, add a partial index shaped to match the
query exactly — a functional index on COALESCE(responded_at, received_at),
scoped by the same status predicate the query itself filters on (a partial
index's WHERE clause only needs to be implied by the query's WHERE clause,
not identical, but making it identical keeps this migration's intent
legible without needing to re-derive it from mailbox_dispatcher.py).

PR #48 review finding: both DDL statements now use CONCURRENTLY. Per
CLAUDE.md there is no separate staging database — migrations run directly
against the live server — and inbound_messages is get_active_mailbox_for_
client()'s own table, called on every single outbound send attempt. A
plain (non-CONCURRENTLY) CREATE/DROP INDEX takes a lock that blocks writes
to the table for the build's duration; at today's row count that's
effectively instant, but this repo's own docstring above anticipates
growth to hundreds/thousands of rows/day, at which point a future
non-CONCURRENTLY DDL statement on this table would measurably stall live
mailbox-picking/inserts. CONCURRENTLY cannot run inside a transaction
block, so this script uses the owner engine directly with an
AUTOCOMMIT-isolation connection instead of get_owner_db_context()'s
Session (the first use of that pattern in this repo — every other
migration's DDL is small/rare enough not to need it).

CONCURRENTLY's own failure mode, for the record: if a concurrent index
build is interrupted (killed, or errors partway), Postgres leaves behind
an INVALID index rather than cleanly rolling back — a subsequent run of
this script's CREATE INDEX CONCURRENTLY IF NOT EXISTS would then no-op
against that invalid index rather than fixing it. Recovery is a manual
`DROP INDEX CONCURRENTLY ix_inbound_messages_mailbox_cap` followed by
re-running this script — not automated here, since detecting and
self-healing an invalid index is more machinery than a one-time index
swap on a still-small table warrants.

Idempotent: DROP INDEX CONCURRENTLY IF EXISTS / CREATE INDEX CONCURRENTLY
IF NOT EXISTS.

Rollback: additive-after-drop — dropping the old index is not reversible
without recreating it by hand (`CREATE INDEX CONCURRENTLY
ix_inbound_messages_mailbox_responded ON inbound_messages (mailbox_id,
responded_at) WHERE mailbox_id IS NOT NULL`), but since nothing else ever
read it, there is no functional reason to.

Run AFTER apply_ack_latency_reconcile.py, BEFORE apply_rls_policies.py (not
tenant-bearing — an index change on an already-registered table).

    PYTHONPATH=. python migrations/apply_mailbox_cap_index_fix.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import db

DDL = [
    "DROP INDEX CONCURRENTLY IF EXISTS ix_inbound_messages_mailbox_responded",
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_inbound_messages_mailbox_cap
        ON inbound_messages (mailbox_id, COALESCE(responded_at, received_at))
        WHERE status IN ('RESPONDED', 'SENT_UNCONFIRMED')
    """,
]


def main() -> None:
    # CONCURRENTLY forbids running inside a transaction block — AUTOCOMMIT
    # isolation makes each statement its own implicitly-committed
    # transaction, which is what CONCURRENTLY requires.
    engine = db.engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for stmt in DDL:
            conn.execute(text(stmt))
    print("apply_mailbox_cap_index_fix: done")


if __name__ == "__main__":
    main()
