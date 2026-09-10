"""Regression tests for the Speed-to-Lead sweep's send/post-send failure
split (Task 4.2.1).

The key invariant: once sender.send() has delivered the auto-response, a
failure in the post-send status write must NEVER reset the row to RECEIVED
(that would resend the same email on the next tick). It goes terminal to
SENT_UNCONFIRMED for manual reconciliation instead. A failure BEFORE the
send is still safe to retry-from-scratch.

These are pure-mock tests — no DB, no SMTP.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

import pytest

import src.tasks.speed_to_lead_sweep as sweep


def _row(msg_id=1):
    return SimpleNamespace(
        id=msg_id,
        client_id="client_a",
        source_channel="webhook",
        sender_name="Pat",
        sender_email="pat@example.com",
        received_at=datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        idempotency_key="client_a:ext1",
    )


class _CtxMgr:
    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *a):
        return False


def test_post_send_write_failure_marks_sent_unconfirmed_not_received():
    """sender.send() succeeds, then _mark_responded raises → row flagged
    SENT_UNCONFIRMED, never reset to RECEIVED (no second send).

    Group D / D-1: the row must still be attributed to the mailbox that
    actually sent it (mailbox_id=9 here), even though the UPDATE that would
    normally have persisted it is exactly what failed — otherwise a
    SENT_UNCONFIRMED row can never count against that mailbox's rolling-24h
    cap (src/services/mailbox_dispatcher.py)."""
    row = _row()
    session = MagicMock()
    # clients template lookup → no custom template
    session.execute.return_value.first.return_value = None

    with patch.object(sweep, "_claim_due", return_value=[row]), \
         patch.object(sweep, "get_system_db_context", return_value=_CtxMgr(MagicMock())), \
         patch.object(sweep, "get_db_context", return_value=_CtxMgr(session)), \
         patch.object(sweep, "resolve_owner_booking_link", return_value=None), \
         patch.object(sweep, "get_active_mailbox_for_client",
                      return_value=SimpleNamespace(mailbox_id=9, mailbox_address="s@a.com", sending_domain="a.com")), \
         patch.object(sweep, "build_email_sender", return_value=MagicMock()), \
         patch.object(sweep, "log_event"), \
         patch.object(sweep, "_mark_responded", side_effect=RuntimeError("db hiccup")), \
         patch.object(sweep, "_mark_sent_unconfirmed") as mark_unconf, \
         patch.object(sweep, "_reset_to_received") as reset:
        sweep.run_sweep()

    mark_unconf.assert_called_once_with(row.id, mailbox_id=9)
    reset.assert_not_called()


def test_pre_send_failure_resets_to_received():
    """Failure before the SMTP send (no mailbox) → row reset to RECEIVED for
    retry, not flagged terminal."""
    row = _row()
    session = MagicMock()
    session.execute.return_value.first.return_value = None

    with patch.object(sweep, "_claim_due", return_value=[row]), \
         patch.object(sweep, "get_system_db_context", return_value=_CtxMgr(MagicMock())), \
         patch.object(sweep, "get_db_context", return_value=_CtxMgr(session)), \
         patch.object(sweep, "resolve_owner_booking_link", return_value=None), \
         patch.object(sweep, "get_active_mailbox_for_client",
                      side_effect=RuntimeError("template blew up")), \
         patch.object(sweep, "build_email_sender", return_value=MagicMock()), \
         patch.object(sweep, "_mark_sent_unconfirmed") as mark_unconf, \
         patch.object(sweep, "_reset_to_received") as reset:
        sweep.run_sweep()

    reset.assert_called_once_with(row.id)
    mark_unconf.assert_not_called()


def test_send_success_marks_responded():
    """Happy path: send + post-send writes succeed → _mark_responded, no
    reset, no terminal flag."""
    row = _row()
    session = MagicMock()
    session.execute.return_value.first.return_value = None

    with patch.object(sweep, "_claim_due", return_value=[row]), \
         patch.object(sweep, "get_system_db_context", return_value=_CtxMgr(MagicMock())), \
         patch.object(sweep, "get_db_context", return_value=_CtxMgr(session)), \
         patch.object(sweep, "resolve_owner_booking_link", return_value=None), \
         patch.object(sweep, "get_active_mailbox_for_client",
                      return_value=SimpleNamespace(mailbox_id=9, mailbox_address="s@a.com", sending_domain="a.com")), \
         patch.object(sweep, "build_email_sender", return_value=MagicMock()), \
         patch.object(sweep, "log_event"), \
         patch.object(sweep, "_mark_responded") as mark_resp, \
         patch.object(sweep, "_mark_sent_unconfirmed") as mark_unconf, \
         patch.object(sweep, "_reset_to_received") as reset:
        dispatched = sweep.run_sweep()

    assert dispatched == 1
    mark_resp.assert_called_once()
    mark_unconf.assert_not_called()
    reset.assert_not_called()


def test_send_success_sets_acked_at_not_ack_latency_seconds():
    """Group D / D-1: _mark_responded must be called with acked_at (a real
    timestamp), never ack_latency_seconds directly — that column is
    GENERATED ALWAYS ... STORED from acked_at, and Postgres rejects a direct
    write to it (ERROR 428C9). This is the regression test for the defect
    itself: before the fix, this call site passed ack_latency=<float>."""
    row = _row()
    session = MagicMock()
    session.execute.return_value.first.return_value = None

    with patch.object(sweep, "_claim_due", return_value=[row]), \
         patch.object(sweep, "get_system_db_context", return_value=_CtxMgr(MagicMock())), \
         patch.object(sweep, "get_db_context", return_value=_CtxMgr(session)), \
         patch.object(sweep, "resolve_owner_booking_link", return_value=None), \
         patch.object(sweep, "get_active_mailbox_for_client",
                      return_value=SimpleNamespace(mailbox_id=9, mailbox_address="s@a.com", sending_domain="a.com")), \
         patch.object(sweep, "build_email_sender", return_value=MagicMock()), \
         patch.object(sweep, "log_event"), \
         patch.object(sweep, "_mark_responded") as mark_resp:
        sweep.run_sweep()

    mark_resp.assert_called_once()
    _, kwargs = mark_resp.call_args
    assert "acked_at" in kwargs, "must pass acked_at, not ack_latency"
    assert isinstance(kwargs["acked_at"], datetime), "acked_at must be a real timestamp"
    assert kwargs["mailbox_id"] == 9


def test_mark_responded_never_writes_ack_latency_seconds_column():
    """The UPDATE built by _mark_responded itself must never reference
    ack_latency_seconds — it is a GENERATED STORED column and any direct
    write to it is rejected by Postgres. Inspect the real SQL text (not the
    mock), since this is precisely the bug: the UPDATE compiled fine in
    Python and only failed against a real database."""
    session = MagicMock()

    sweep._mark_responded(session, "123", acked_at=datetime(2026, 9, 10, tzinfo=timezone.utc), mailbox_id=9)

    sql = str(session.execute.call_args.args[0])
    assert "ack_latency_seconds" not in sql
    assert "acked_at" in sql


def test_no_prospect_email_lands_terminal_not_looping():
    """Group D / D-1 second finding: the no-prospect-email path used to call
    _mark_responded with a write to the GENERATED column, which raised, which
    reset the row back to RECEIVED (a plain Exception, not PostSendError) —
    an infinite RECEIVED→SENDING→RECEIVED loop every sweep tick. After the
    fix, _mark_responded no longer touches the generated column, so this path
    must complete cleanly with no send, no reset, and acked_at left NULL."""
    row = _row(msg_id=2)
    row = SimpleNamespace(**{**row.__dict__, "sender_email": None})
    session = MagicMock()

    with patch.object(sweep, "_claim_due", return_value=[row]), \
         patch.object(sweep, "get_system_db_context", return_value=_CtxMgr(MagicMock())), \
         patch.object(sweep, "get_db_context", return_value=_CtxMgr(session)), \
         patch.object(sweep, "_mark_responded") as mark_resp, \
         patch.object(sweep, "_reset_to_received") as reset, \
         patch.object(sweep, "_mark_sent_unconfirmed") as mark_unconf:
        dispatched = sweep.run_sweep()

    assert dispatched == 1
    mark_resp.assert_called_once_with(session, "2", acked_at=None, mailbox_id=None)
    reset.assert_not_called()
    mark_unconf.assert_not_called()


def test_claim_due_excludes_human_review_rows():
    """PR finding: _claim_due must never claim requires_human_review rows, so
    an unparsed/ambiguous notification is never auto-responded (which could
    reply to the portal). Assert the guard is in the claim SQL."""
    session = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = []
    session.execute.return_value = result

    sweep._claim_due(session, limit=10)

    sql = str(session.execute.call_args.args[0])
    assert "requires_human_review = FALSE" in sql
