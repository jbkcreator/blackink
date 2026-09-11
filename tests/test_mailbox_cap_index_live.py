"""Live-DB schema check for the mailbox-cap index fix (post-review finding
on Group D defect D-1).

D-1's mailbox_dispatcher.py change (COALESCE(responded_at, received_at),
widened to include SENT_UNCONFIRMED) made the pre-existing
ix_inbound_messages_mailbox_responded index unusable for that query —
confirmed via EXPLAIN against the live DB (Seq Scan, index not considered).
migrations/apply_mailbox_cap_index_fix.py replaces it with an index shaped
to match. This is a schema-existence check, not a query-plan check (a plan
depends on live table statistics/row counts this repo's disposable test
DBs won't have) — the closest structural proof available for "the covering
index exists," per this repo's own preference for a real assertion over a
code-review claim.

PR #48 x main merge (2.1.3 KB Auto-Response Engine): the cap subquery this
index serves now also counts in-flight 'SENDING' rows and falls back
through claimed_at before received_at (see mailbox_dispatcher.py), so
neither the v1 index's 2-status/2-arg-COALESCE shape nor its name
(ix_inbound_messages_mailbox_cap) match anymore — the migration replaced
it with ix_inbound_messages_mailbox_cap_v2, asserted below.

Requires a real Postgres with migrations applied, same class as
tests/test_tenant_isolation.py.
"""
from sqlalchemy import text

from src.core.database import get_system_db_context


def test_old_unusable_indexes_are_gone():
    with get_system_db_context() as session:
        rows = session.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'inbound_messages' "
            "AND indexname IN ('ix_inbound_messages_mailbox_responded', "
            "'ix_inbound_messages_mailbox_cap')"
        )).fetchall()
    assert rows == [], (
        f"stale index(es) still exist: {[r[0] for r in rows]} — neither "
        "covers the merged mailbox cap query shape (COALESCE(responded_at, "
        "claimed_at, received_at) over status IN ('SENDING','RESPONDED', "
        "'SENT_UNCONFIRMED')) and neither has any other caller; leaving them "
        "around is dead weight, not a safety net."
    )


def test_new_index_covers_the_actual_cap_query_shape():
    with get_system_db_context() as session:
        row = session.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'inbound_messages' "
            "AND indexname = 'ix_inbound_messages_mailbox_cap_v2'"
        )).first()
    assert row is not None, "ix_inbound_messages_mailbox_cap_v2 is missing"
    indexdef = row[0]
    assert "mailbox_id" in indexdef
    assert "COALESCE(responded_at, claimed_at, received_at)" in indexdef
    assert "SENDING" in indexdef and "RESPONDED" in indexdef and "SENT_UNCONFIRMED" in indexdef
