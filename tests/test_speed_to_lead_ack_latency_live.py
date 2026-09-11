"""Live-DB regression test for Group D defect D-1 (Week 0-2 implementation
audit): inbound_messages.ack_latency_seconds is a GENERATED ALWAYS ... STORED
column derived from acked_at (apply_entitlements_billing.py +
apply_ack_latency_reconcile.py). speed_to_lead_sweep.py must never write to
it directly — Postgres rejects that with ERROR 428C9.

Requires a real Postgres with migrations applied, same class as
tests/test_tenant_isolation.py. FakeSession-based tests
(tests/test_speed_to_lead_sweep.py) cover the call-site behaviour; this test
proves the actual UPDATE succeeds against a real schema and that the
generated column derives the correct value, which a mock can't demonstrate.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.services.events import log_event
from src.tasks.speed_to_lead_sweep import _mark_responded, _mark_sent_unconfirmed
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401

# The exact rolling-24h predicate from
# src/services/mailbox_dispatcher.py::get_active_mailbox_for_client's cap
# subquery — duplicated verbatim (not imported) since it lives inline in a
# larger correlated subquery there, not as a standalone callable. Kept
# identical on purpose: this is a regression test on the SQL shape itself.
_CAP_COUNT_SQL = (
    "SELECT COUNT(*) FROM inbound_messages im "
    "WHERE im.mailbox_id = :mailbox_id "
    "  AND im.status IN ('RESPONDED', 'SENT_UNCONFIRMED') "
    "  AND COALESCE(im.responded_at, im.received_at) >= NOW() - INTERVAL '24 hours'"
)


@pytest.fixture
def stl_message(canary_tenants):
    """A real inbound_messages row in RECEIVED status under a real client_id,
    with cleanup that runs before the canary teardown (client_id FK)."""
    ctx = canary_tenants[CANARY_A]
    received_at = datetime.now(timezone.utc) - timedelta(seconds=45)
    with get_system_db_context() as s:
        message_id = s.execute(
            text(
                "INSERT INTO inbound_messages "
                "(client_id, idempotency_key, destination_address, sender_email, "
                " sender_name, received_at, status, channel, send_at) "
                "VALUES (:client_id, :idem, 'leads@test.getblackink.com', :email, "
                " 'Pat', :received_at, 'RECEIVED', 'EMAIL', :received_at) "
                "RETURNING id"
            ),
            {
                "client_id": CANARY_A,
                "idem": f"{CANARY_A}:d1-live-test",
                "email": f"pat@{ctx['domain']}",
                "received_at": received_at,
            },
        ).scalar()
        s.commit()

    yield {"id": message_id, "client_id": CANARY_A, "received_at": received_at}

    with get_owner_db_context() as s:
        s.execute(text("DELETE FROM inbound_messages WHERE id = :id"), {"id": message_id})
        s.execute(text("DELETE FROM events WHERE client_id = :c AND entity_id = :id"),
                   {"c": CANARY_A, "id": str(message_id)})


def test_mark_responded_succeeds_and_ack_latency_derives_correctly(stl_message):
    """The real regression: _mark_responded's UPDATE (setting acked_at, never
    ack_latency_seconds) must succeed against the real schema, the row must
    reach RESPONDED, and the GENERATED column must derive a value consistent
    with acked_at - received_at."""
    message_id = stl_message["id"]
    client_id = stl_message["client_id"]
    acked_at = datetime.now(timezone.utc)

    with get_system_db_context() as session:
        # Mirrors the real call sequence in _send_response: the event write
        # shares the same transaction/commit as the status update, so both
        # survive or neither does (this is exactly what silently rolled back
        # before the fix, when the UPDATE below raised).
        log_event(
            client_id,
            "speed_to_lead_response_sent",
            entity_type="inbound_message",
            entity_id=str(message_id),
            payload={"message_id": str(message_id), "ack_latency_seconds": 45.0},
            session=session,
        )
        _mark_responded(session, str(message_id), acked_at=acked_at, mailbox_id=None)

    with get_system_db_context() as session:
        row = session.execute(
            text(
                "SELECT status, acked_at, ack_latency_seconds, responded_at "
                "FROM inbound_messages WHERE id = :id"
            ),
            {"id": message_id},
        ).one()
        assert row.status == "RESPONDED"
        assert row.acked_at is not None
        assert row.responded_at is not None
        # GENERATED ALWAYS AS (EXTRACT(EPOCH FROM (acked_at - received_at))::INTEGER) STORED
        expected = int((row.acked_at - stl_message["received_at"]).total_seconds())
        assert row.ack_latency_seconds == expected

        event = session.execute(
            text(
                "SELECT event_type FROM events "
                "WHERE client_id = :c AND entity_id = :id AND event_type = 'speed_to_lead_response_sent'"
            ),
            {"c": client_id, "id": str(message_id)},
        ).first()
        assert event is not None, "event must survive the same commit as the status update"


def test_ack_latency_seconds_column_is_generated_not_writable(stl_message):
    """Schema-level assertion of the fix itself: a direct write to
    ack_latency_seconds must be rejected by Postgres. If this ever starts
    passing, the migration reconciliation (apply_ack_latency_reconcile.py)
    or apply_entitlements_billing.py has regressed to a plain column."""
    from sqlalchemy.exc import DBAPIError

    with get_system_db_context() as session:
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE inbound_messages SET ack_latency_seconds = 99 WHERE id = :id"),
                {"id": stl_message["id"]},
            )
        session.rollback()


@pytest.fixture
def stl_message_stuck_over_24h(canary_tenants):
    """A real inbound_messages row that waited >24h for mailbox capacity
    before finally being claimed and sent — received_at is deliberately 25h
    in the past, and status is SENDING (the sweep's in-progress sentinel,
    what _mark_sent_unconfirmed's WHERE clause requires) to simulate the
    exact moment right after sender.send() succeeded but before the
    post-send bookkeeping write."""
    ctx = canary_tenants[CANARY_A]
    received_at = datetime.now(timezone.utc) - timedelta(hours=25)
    with get_system_db_context() as s:
        message_id = s.execute(
            text(
                "INSERT INTO inbound_messages "
                "(client_id, idempotency_key, destination_address, sender_email, "
                " sender_name, received_at, status, channel, send_at) "
                "VALUES (:client_id, :idem, 'leads@test.getblackink.com', :email, "
                " 'Pat', :received_at, 'SENDING', 'EMAIL', :received_at) "
                "RETURNING id"
            ),
            {
                "client_id": CANARY_A,
                "idem": f"{CANARY_A}:d1-cap-bypass-test",
                "email": f"pat@{ctx['domain']}",
                "received_at": received_at,
            },
        ).scalar()
        s.commit()

    yield {"id": message_id, "client_id": CANARY_A, "received_at": received_at}

    with get_owner_db_context() as s:
        s.execute(text("DELETE FROM inbound_messages WHERE id = :id"), {"id": message_id})


def test_sent_unconfirmed_with_stale_received_at_still_counts_against_mailbox_cap(
    stl_message_stuck_over_24h,
):
    """PR #48 re-review finding: a lead that waited >24h for mailbox
    capacity, then hit a post-send bookkeeping failure right after actually
    sending, must still count against that mailbox's rolling-24h cap. Before
    this fix, _mark_sent_unconfirmed left responded_at NULL, so
    mailbox_dispatcher.py's COALESCE(responded_at, received_at) fell back to
    the 25h-old received_at — outside the window — making a send that
    happened SECONDS ago invisible to the cap query immediately."""
    message_id = stl_message_stuck_over_24h["id"]
    mailbox_id = 999_001  # synthetic — inbound_messages.mailbox_id has no FK
    sent_at = datetime.now(timezone.utc)

    _mark_sent_unconfirmed(message_id, mailbox_id=mailbox_id, sent_at=sent_at)

    with get_system_db_context() as session:
        row = session.execute(
            text(
                "SELECT status, mailbox_id, responded_at, acked_at, received_at "
                "FROM inbound_messages WHERE id = :id"
            ),
            {"id": message_id},
        ).one()
        assert row.status == "SENT_UNCONFIRMED"
        assert row.mailbox_id == mailbox_id
        assert row.responded_at is not None, "responded_at must be backfilled from sent_at"
        assert row.acked_at is not None, "acked_at must be backfilled from sent_at"
        # received_at is untouched and genuinely still 25h old — proving the
        # cap query below only passes because of the responded_at backfill,
        # not because received_at was somehow also fresh.
        assert (datetime.now(timezone.utc) - row.received_at).total_seconds() > 24 * 3600

        cap_count = session.execute(
            text(_CAP_COUNT_SQL), {"mailbox_id": mailbox_id}
        ).scalar()
        assert cap_count == 1, (
            "a SENT_UNCONFIRMED row from a send that happened seconds ago must "
            "count against the mailbox's rolling-24h cap, regardless of how "
            "stale its received_at is"
        )


def test_sent_unconfirmed_without_sent_at_does_not_backfill(canary_tenants):
    """Control: if sent_at is genuinely unknown (None), _mark_sent_unconfirmed
    must not fabricate a timestamp — COALESCE(:sent_at, responded_at) leaves
    an already-NULL responded_at NULL rather than guessing. This is the
    legitimate case of a failure so early that no send timestamp was ever
    captured (should not occur post-fix, but the function must not crash or
    silently invent data if it does)."""
    ctx = canary_tenants[CANARY_A]
    with get_system_db_context() as s:
        message_id = s.execute(
            text(
                "INSERT INTO inbound_messages "
                "(client_id, idempotency_key, destination_address, sender_email, "
                " sender_name, received_at, status, channel, send_at) "
                "VALUES (:client_id, :idem, 'leads@test.getblackink.com', :email, "
                " 'Pat', NOW(), 'SENDING', 'EMAIL', NOW()) "
                "RETURNING id"
            ),
            {
                "client_id": CANARY_A,
                "idem": f"{CANARY_A}:d1-no-sent-at-test",
                "email": f"pat@{ctx['domain']}",
            },
        ).scalar()
        s.commit()

    try:
        _mark_sent_unconfirmed(message_id, mailbox_id=42, sent_at=None)
        with get_system_db_context() as session:
            row = session.execute(
                text("SELECT status, mailbox_id, responded_at FROM inbound_messages WHERE id = :id"),
                {"id": message_id},
            ).one()
            assert row.status == "SENT_UNCONFIRMED"
            assert row.mailbox_id == 42
            assert row.responded_at is None
    finally:
        with get_owner_db_context() as s:
            s.execute(text("DELETE FROM inbound_messages WHERE id = :id"), {"id": message_id})
