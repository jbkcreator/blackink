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
    SENT_UNCONFIRMED, never reset to RECEIVED (no second send)."""
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

    mark_unconf.assert_called_once_with(row.id)
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
