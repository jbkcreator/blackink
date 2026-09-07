"""Tests for sequence_dispatcher — at-most-once INSERT→send→UPDATE pattern."""

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from src.services.sequence_dispatcher import (
    claim_touch,
    daily_sends_for_mailbox,
    mark_failed,
    mark_sent,
)


# ---------------------------------------------------------------------------
# FakeSession helpers
# ---------------------------------------------------------------------------

def _exec_returning(rowcount=1, scalar_val=None, fetchone_row=None):
    result = MagicMock()
    result.rowcount = rowcount
    result.scalar = lambda: scalar_val
    result.fetchone = lambda: fetchone_row
    return result


def _session_claim_success():
    """INSERT succeeds — returns a row with dispatch_id."""
    import uuid
    _id = str(uuid.uuid4())
    session = MagicMock()
    # fetchone returns a row with dispatch_id
    result = MagicMock()
    result.fetchone = lambda: SimpleNamespace(dispatch_id=_id)
    session.execute.return_value = result
    session._expected_id = _id
    return session


def _session_claim_conflict():
    """INSERT ON CONFLICT DO NOTHING — fetchone returns None."""
    session = MagicMock()
    result = MagicMock()
    result.fetchone = lambda: None
    session.execute.return_value = result
    return session


def _session_update(rowcount=1):
    session = MagicMock()
    session.execute.return_value = _exec_returning(rowcount=rowcount)
    return session


def _session_scalar(value):
    session = MagicMock()
    session.execute.return_value = _exec_returning(scalar_val=value)
    return session


# ---------------------------------------------------------------------------
# claim_touch
# ---------------------------------------------------------------------------

def test_claim_touch_returns_dispatch_id_on_success():
    session = _session_claim_success()
    result = claim_touch(session, "client_a", "run-uuid", 1, mailbox_id=7)
    assert result == session._expected_id


def test_claim_touch_returns_none_on_conflict():
    session = _session_claim_conflict()
    result = claim_touch(session, "client_a", "run-uuid", 1, mailbox_id=7)
    assert result is None


def test_claim_touch_sql_contains_on_conflict():
    session = _session_claim_success()
    claim_touch(session, "client_a", "run-uuid", 1, mailbox_id=7)
    sql = str(session.execute.call_args_list[0][0][0]).upper()
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql


def test_claim_touch_includes_client_id_in_params():
    session = _session_claim_success()
    claim_touch(session, "client_xyz", "run-uuid", 3, mailbox_id=2)
    params = session.execute.call_args_list[0][0][1]
    assert params["client_id"] == "client_xyz"


# ---------------------------------------------------------------------------
# mark_sent
# ---------------------------------------------------------------------------

def test_mark_sent_returns_true_on_success():
    session = _session_update(rowcount=1)
    assert mark_sent(session, "client_a", "dispatch-uuid", "<msg@domain>") is True


def test_mark_sent_returns_false_when_row_gone():
    session = _session_update(rowcount=0)
    assert mark_sent(session, "client_a", "dispatch-uuid", "<msg@domain>") is False


def test_mark_sent_sets_status_sent():
    session = _session_update()
    mark_sent(session, "client_a", "dispatch-uuid", "<msg@domain>")
    sql = str(session.execute.call_args_list[0][0][0]).upper()
    assert "SENT" in sql
    assert "MESSAGE_ID" in sql or "message_id" in str(session.execute.call_args_list[0][0][0])


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

def test_mark_failed_executes_update():
    session = _session_update()
    mark_failed(session, "client_a", "dispatch-uuid", "smtp error")
    sql = str(session.execute.call_args_list[0][0][0]).upper()
    assert "UPDATE" in sql
    assert "FAILED" in sql


def test_mark_failed_includes_client_id():
    session = _session_update()
    mark_failed(session, "client_b", "dispatch-uuid", "timeout")
    params = session.execute.call_args_list[0][0][1]
    assert params["client_id"] == "client_b"


# ---------------------------------------------------------------------------
# daily_sends_for_mailbox
# ---------------------------------------------------------------------------

def test_daily_sends_returns_count():
    session = _session_scalar(37)
    assert daily_sends_for_mailbox(session, mailbox_id=5, client_id="client_a") == 37


def test_daily_sends_returns_zero_when_none():
    session = _session_scalar(None)
    assert daily_sends_for_mailbox(session, mailbox_id=5, client_id="client_a") == 0


def test_daily_sends_sql_filters_24h():
    session = _session_scalar(0)
    daily_sends_for_mailbox(session, mailbox_id=5, client_id="client_a")
    sql = str(session.execute.call_args_list[0][0][0])
    assert "24" in sql or "INTERVAL" in sql.upper()
