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

Requires a real Postgres with migrations applied, same class as
tests/test_tenant_isolation.py.
"""
from sqlalchemy import text

from src.core.database import get_system_db_context


def test_old_unusable_index_is_gone():
    with get_system_db_context() as session:
        row = session.execute(text(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'inbound_messages' "
            "AND indexname = 'ix_inbound_messages_mailbox_responded'"
        )).first()
    assert row is None, (
        "ix_inbound_messages_mailbox_responded still exists — it no longer "
        "covers the mailbox cap query (COALESCE defeats a plain btree index "
        "on responded_at) and has no other caller; leaving it around is "
        "dead weight, not a safety net."
    )


def test_new_index_covers_the_actual_cap_query_shape():
    with get_system_db_context() as session:
        row = session.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'inbound_messages' "
            "AND indexname = 'ix_inbound_messages_mailbox_cap'"
        )).first()
    assert row is not None, "ix_inbound_messages_mailbox_cap is missing"
    indexdef = row[0]
    assert "mailbox_id" in indexdef
    assert "COALESCE(responded_at, received_at)" in indexdef
    assert "RESPONDED" in indexdef and "SENT_UNCONFIRMED" in indexdef
