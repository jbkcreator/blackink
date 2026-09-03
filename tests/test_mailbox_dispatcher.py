"""Tests for per-tenant mailbox dispatcher.

Dev 4 / Week 0: strict client_id isolation — no cross-tenant bleed.
FakeSession pattern, no live DB.
"""

from unittest.mock import MagicMock, call
import pytest

from src.services.mailbox_dispatcher import (
    NoMailboxAvailable,
    MailboxAssignment,
    get_active_mailbox_for_client,
)


# ---------------------------------------------------------------------------
# FakeSession helpers
# ---------------------------------------------------------------------------

def _session_with_mailbox(
    mailbox_id=1,
    address="sales1@clienta.com",
    instantly_email="sales1@clienta.com",
    client_id="client_a",
):
    session = MagicMock()
    select_result = MagicMock()
    select_result.fetchone.return_value = (mailbox_id, address, instantly_email, client_id)
    update_result = MagicMock()
    session.execute.side_effect = [select_result, update_result]
    return session


def _session_no_mailbox():
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = None
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_returns_mailbox_assignment():
    session = _session_with_mailbox(mailbox_id=7, address="sales1@acme.com", client_id="client_a")
    result = get_active_mailbox_for_client(session, "client_a")
    assert isinstance(result, MailboxAssignment)
    assert result.mailbox_id == 7
    assert result.mailbox_address == "sales1@acme.com"
    assert result.client_id == "client_a"


def test_updates_last_used_at_after_pick():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    # Second execute call must be the UPDATE
    assert session.execute.call_count == 2
    update_sql = str(session.execute.call_args_list[1][0][0]).upper()
    assert "UPDATE" in update_sql
    assert "LAST_USED_AT" in update_sql


def test_instantly_account_email_returned():
    session = _session_with_mailbox(instantly_email="warmbox@tenant.io", client_id="client_a")
    result = get_active_mailbox_for_client(session, "client_a")
    assert result.instantly_account_email == "warmbox@tenant.io"


def test_instantly_account_email_can_be_none():
    session = _session_with_mailbox(instantly_email=None, client_id="client_a")
    result = get_active_mailbox_for_client(session, "client_a")
    assert result.instantly_account_email is None


# ---------------------------------------------------------------------------
# No mailbox available
# ---------------------------------------------------------------------------

def test_raises_no_mailbox_available_when_none():
    with pytest.raises(NoMailboxAvailable, match="client_id=client_a"):
        get_active_mailbox_for_client(_session_no_mailbox(), "client_a")


def test_error_message_mentions_client_id():
    with pytest.raises(NoMailboxAvailable, match="client_a"):
        get_active_mailbox_for_client(_session_no_mailbox(), "client_a")


def test_no_update_when_no_mailbox():
    session = _session_no_mailbox()
    with pytest.raises(NoMailboxAvailable):
        get_active_mailbox_for_client(session, "client_a")
    # Only one execute call (the SELECT) — no UPDATE should happen
    assert session.execute.call_count == 1


# ---------------------------------------------------------------------------
# Cross-tenant breach guard
# ---------------------------------------------------------------------------

def test_raises_on_client_id_mismatch():
    """DB returned a mailbox with wrong client_id — must hard-error, never send."""
    session = _session_with_mailbox(client_id="client_b")  # wrong tenant in DB row
    with pytest.raises(RuntimeError, match="CROSS-TENANT BREACH"):
        get_active_mailbox_for_client(session, "client_a")


def test_breach_error_names_both_client_ids():
    session = _session_with_mailbox(client_id="client_b")
    with pytest.raises(RuntimeError, match="client_b"):
        get_active_mailbox_for_client(session, "client_a")


# ---------------------------------------------------------------------------
# Query shape
# ---------------------------------------------------------------------------

def test_query_filters_by_client_id():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    select_params = session.execute.call_args_list[0][0][1]
    assert select_params["client_id"] == "client_a"


def test_query_uses_for_update_skip_locked():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    select_sql = str(session.execute.call_args_list[0][0][0]).upper()
    assert "FOR UPDATE SKIP LOCKED" in select_sql


def test_query_filters_warmed_and_active():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    select_sql = str(session.execute.call_args_list[0][0][0])
    assert "warmed" in select_sql
    assert "active" in select_sql
