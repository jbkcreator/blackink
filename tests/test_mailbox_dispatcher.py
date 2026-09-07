"""Tests for per-tenant mailbox dispatcher.

Updated for Task 3.1.1: dispatcher now joins sending_domains to enforce
domain-level quarantine. MailboxAssignment gains sending_domain. Two new
exception subtypes: AllMailboxesQuarantined and NoWarmedMailbox.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from src.services.mailbox_dispatcher import (
    AllMailboxesQuarantined,
    MailboxAssignment,
    NoMailboxAvailable,
    NoWarmedMailbox,
    get_active_mailbox_for_client,
)


# ---------------------------------------------------------------------------
# FakeSession helpers
# ---------------------------------------------------------------------------

def _scalar(value):
    return SimpleNamespace(scalar=lambda: value)


def _fetchone(row):
    return SimpleNamespace(fetchone=lambda: row)


def _session_with_mailbox(
    mailbox_id=1,
    address="sales1@clienta.com",
    instantly_email="sales1@clienta.com",
    client_id="client_a",
    domain="clienta-outreach.com",
    daily_send_ceiling=0,
):
    """Session that returns one warmed mailbox joined to an active domain.

    execute() call order (cap resolved from clients when caller passes none):
      0. SELECT clients.daily_send_ceiling → ceiling (0 → fallback default)
      1. COUNT warmed mailboxes → 1
      2. SELECT ... JOIN sending_domains → row
      3. UPDATE last_used_at → (ignored)
    """
    session = MagicMock()
    session.execute.side_effect = [
        _scalar(daily_send_ceiling),                                    # clients ceiling
        _scalar(1),                                                     # warmed count
        _fetchone((mailbox_id, address, instantly_email, client_id, domain)),  # JOIN row
        MagicMock(),                                                    # UPDATE
    ]
    return session


def _session_no_mailbox(warmed_count=0, any_count=0, warmed_active_count=0, daily_send_ceiling=0):
    """Session where the (warmed + active + under-cap) JOIN finds nothing.

    execute() call order (cap resolved from clients when caller passes none):
      0. SELECT clients.daily_send_ceiling → ceiling
      1. COUNT warmed mailboxes
      2. SELECT ... JOIN (cap-filtered) → None
      3. COUNT warmed + active mailboxes (ignoring cap) — distinguishes "all
         capped" from "all quarantined"
      4. COUNT all mailboxes (only reached if not capped and warmed_count == 0)
    """
    session = MagicMock()
    calls = [_scalar(daily_send_ceiling), _scalar(warmed_count), _fetchone(None), _scalar(warmed_active_count)]
    if warmed_active_count == 0 and warmed_count == 0:
        calls.append(_scalar(any_count))
    session.execute.side_effect = calls
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


def test_sending_domain_included_in_assignment():
    session = _session_with_mailbox(domain="outreach.blackink.io", client_id="client_a")
    result = get_active_mailbox_for_client(session, "client_a")
    assert result.sending_domain == "outreach.blackink.io"


def test_updates_last_used_at_after_pick():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    # Fourth execute call must be the UPDATE (index 0 is the clients ceiling lookup)
    assert session.execute.call_count == 4
    update_sql = str(session.execute.call_args_list[3][0][0]).upper()
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
# No mailbox — specific causes
# ---------------------------------------------------------------------------

def test_raises_all_mailboxes_quarantined_when_warmed_exist_but_domain_quarantined():
    """Warmed mailboxes exist but all their domains are quarantined → specific error."""
    session = _session_no_mailbox(warmed_count=2, any_count=2)
    with pytest.raises(AllMailboxesQuarantined):
        get_active_mailbox_for_client(session, "client_a")


def test_all_mailboxes_quarantined_is_subclass_of_no_mailbox_available():
    session = _session_no_mailbox(warmed_count=2, any_count=2)
    with pytest.raises(NoMailboxAvailable):
        get_active_mailbox_for_client(session, "client_a")


def test_raises_all_mailboxes_capped_when_warmed_active_all_at_cap():
    """Warmed, un-quarantined mailboxes exist but all at 24h cap → AllMailboxesCapped."""
    from src.services.mailbox_dispatcher import AllMailboxesCapped
    session = _session_no_mailbox(warmed_count=2, warmed_active_count=2)
    with pytest.raises(AllMailboxesCapped):
        get_active_mailbox_for_client(session, "client_a")


def test_all_mailboxes_capped_is_subclass_of_no_mailbox_available():
    session = _session_no_mailbox(warmed_count=2, warmed_active_count=2)
    with pytest.raises(NoMailboxAvailable):
        get_active_mailbox_for_client(session, "client_a")


def test_raises_no_warmed_mailbox_when_unwarmed_mailboxes_exist():
    """Mailboxes provisioned but none warmed → specific error."""
    session = _session_no_mailbox(warmed_count=0, any_count=3)
    with pytest.raises(NoWarmedMailbox):
        get_active_mailbox_for_client(session, "client_a")


def test_raises_no_mailbox_available_when_nothing_provisioned():
    session = _session_no_mailbox(warmed_count=0, any_count=0)
    with pytest.raises(NoMailboxAvailable, match="No mailboxes provisioned"):
        get_active_mailbox_for_client(session, "client_a")


def test_no_update_when_no_mailbox():
    session = _session_no_mailbox(warmed_count=0, any_count=0)
    with pytest.raises(NoMailboxAvailable):
        get_active_mailbox_for_client(session, "client_a")
    # Match the real last_used_at write, not the SELECT's "FOR UPDATE" clause.
    update_calls = [
        c for c in session.execute.call_args_list
        if "SET LAST_USED_AT" in str(c[0][0]).upper()
    ]
    assert len(update_calls) == 0


# ---------------------------------------------------------------------------
# Cross-tenant breach guard
# ---------------------------------------------------------------------------

def test_raises_on_client_id_mismatch():
    session = _session_with_mailbox(client_id="client_b")
    with pytest.raises(RuntimeError, match="CROSS-TENANT BREACH"):
        get_active_mailbox_for_client(session, "client_a")


def test_breach_error_names_both_client_ids():
    session = _session_with_mailbox(client_id="client_b")
    with pytest.raises(RuntimeError, match="client_b"):
        get_active_mailbox_for_client(session, "client_a")


# ---------------------------------------------------------------------------
# Query shape — domain quarantine fix
# ---------------------------------------------------------------------------

def test_query_joins_sending_domains():
    """Dispatcher must join sending_domains — the key fix for the sentinel bug."""
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[2][0][0])
    assert "sending_domains" in join_sql


def test_query_checks_domain_quarantine_state():
    """Must check sd.quarantine_state, not just mailboxes.quarantine_state."""
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[2][0][0])
    assert "sd.quarantine_state" in join_sql or "sending_domains" in join_sql


def test_query_filters_by_client_id():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_params = session.execute.call_args_list[2][0][1]
    assert join_params["client_id"] == "client_a"


def test_query_uses_for_update_skip_locked():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[2][0][0]).upper()
    assert "FOR UPDATE" in join_sql
    assert "SKIP LOCKED" in join_sql


def test_query_filters_warmed_and_active_domain():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[2][0][0])
    assert "warmed" in join_sql
    assert "active" in join_sql


# ---------------------------------------------------------------------------
# Per-client daily send ceiling (wayfinder dev4-week2 ticket 01)
# ---------------------------------------------------------------------------

def test_cap_resolved_from_client_ceiling_when_caller_passes_none():
    """A positive clients.daily_send_ceiling becomes the cap in the JOIN query."""
    session = _session_with_mailbox(client_id="client_a", daily_send_ceiling=30)
    get_active_mailbox_for_client(session, "client_a")
    # call 0 = ceiling lookup against clients; call 2 = the JOIN pick with :cap bound
    ceiling_sql = str(session.execute.call_args_list[0][0][0]).lower()
    assert "daily_send_ceiling" in ceiling_sql and "clients" in ceiling_sql
    assert session.execute.call_args_list[2][0][1]["cap"] == 30


def test_zero_ceiling_falls_back_to_default_cap():
    """daily_send_ceiling = 0 means 'unset' → DEFAULT_DAILY_SEND_CAP, never a 0 floor."""
    from src.services.mailbox_dispatcher import DEFAULT_DAILY_SEND_CAP
    session = _session_with_mailbox(client_id="client_a", daily_send_ceiling=0)
    get_active_mailbox_for_client(session, "client_a")
    assert session.execute.call_args_list[2][0][1]["cap"] == DEFAULT_DAILY_SEND_CAP


def test_explicit_cap_argument_skips_client_lookup():
    """An explicit daily_send_cap wins and does not query clients at all."""
    session = MagicMock()
    session.execute.side_effect = [
        _scalar(1),                                                          # warmed count
        _fetchone((1, "a@x.com", "a@x.com", "client_a", "x.com")),           # JOIN row
        MagicMock(),                                                         # UPDATE
    ]
    get_active_mailbox_for_client(session, "client_a", daily_send_cap=17)
    # No clients ceiling lookup — first call is the warmed-count COUNT.
    assert "daily_send_ceiling" not in str(session.execute.call_args_list[0][0][0]).lower()
    assert session.execute.call_args_list[1][0][1]["cap"] == 17
