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
    client_24h_count=0,
):
    """Session that returns one warmed mailbox joined to an active domain.

    execute() call order:
      0. SELECT pg_advisory_xact_lock(...)      — per-client serialize (ignored)
      1. SELECT clients.daily_send_ceiling      — per-client ceiling (0 = none)
      [1b. COUNT client 24h sends — only when ceiling > 0]
      2. COUNT warmed mailboxes → 1
      3. SELECT ... JOIN sending_domains → row
      4. UPDATE last_used_at → (ignored)
    (indices shift +1 when ceiling > 0 inserts the 24h count)
    """
    session = MagicMock()
    calls = [MagicMock(), _scalar(daily_send_ceiling)]              # advisory lock, ceiling
    if daily_send_ceiling and daily_send_ceiling > 0:
        calls.append(_scalar(client_24h_count))                    # client 24h total
    calls += [
        _scalar(1),                                                # warmed count
        _fetchone((mailbox_id, address, instantly_email, client_id, domain)),  # JOIN row
        MagicMock(),                                               # UPDATE
    ]
    session.execute.side_effect = calls
    return session


def _session_no_mailbox(warmed_count=0, any_count=0, warmed_active_count=0, daily_send_ceiling=0):
    """Session where the (warmed + active + under-cap) JOIN finds nothing.

    execute() call order (ceiling = 0, so no 24h count):
      0. SELECT pg_advisory_xact_lock(...)  — per-client serialize (ignored)
      1. SELECT clients.daily_send_ceiling  — 0 (no aggregate cap)
      2. COUNT warmed mailboxes
      3. SELECT ... JOIN (cap-filtered) → None
      4. COUNT warmed + active mailboxes (ignoring cap) — "capped" vs "quarantined"
      5. COUNT all mailboxes (only reached if not capped and warmed_count == 0)
    """
    session = MagicMock()
    calls = [MagicMock(), _scalar(daily_send_ceiling), _scalar(warmed_count), _fetchone(None), _scalar(warmed_active_count)]
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
    # Order: 0 advisory-lock, 1 ceiling, 2 warmed-count, 3 JOIN pick, 4 UPDATE.
    assert session.execute.call_count == 5
    update_sql = str(session.execute.call_args_list[4][0][0]).upper()
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
    join_sql = str(session.execute.call_args_list[3][0][0])
    assert "sending_domains" in join_sql


def test_query_checks_domain_quarantine_state():
    """Must check sd.quarantine_state, not just mailboxes.quarantine_state."""
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[3][0][0])
    assert "sd.quarantine_state" in join_sql or "sending_domains" in join_sql


def test_query_filters_by_client_id():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_params = session.execute.call_args_list[3][0][1]
    assert join_params["client_id"] == "client_a"


def test_query_uses_for_update_skip_locked():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[3][0][0]).upper()
    assert "FOR UPDATE" in join_sql
    assert "SKIP LOCKED" in join_sql


def test_query_filters_warmed_and_active_domain():
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[3][0][0])
    assert "warmed" in join_sql
    assert "active" in join_sql


def test_cap_query_counts_sent_unconfirmed_inbound_messages():
    """Group D / D-1 fix: a Speed-to-Lead auto-response whose post-send write
    failed (status SENT_UNCONFIRMED) already went out over SMTP — it must
    still count against this mailbox's rolling-24h cap, or repeated
    post-send failures let sends bypass the cap entirely."""
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[3][0][0])
    assert "SENT_UNCONFIRMED" in join_sql
    assert "im.status IN ('RESPONDED', 'SENT_UNCONFIRMED')" in join_sql


def test_cap_query_falls_back_to_received_at_for_unconfirmed_rows():
    """responded_at is never set on a SENT_UNCONFIRMED row (the UPDATE that
    would have set it is what failed) — the 24h window must fall back to
    received_at, which is always set, or such rows would never expire from
    (or ever enter) the cap window."""
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    join_sql = str(session.execute.call_args_list[3][0][0])
    assert "COALESCE(im.responded_at, im.claimed_at, im.received_at)" in join_sql


# ---------------------------------------------------------------------------
# Per-mailbox cap + per-client daily ceiling (PR #35 review)
# ---------------------------------------------------------------------------

def test_per_mailbox_cap_defaults_to_platform_max():
    """With no ceiling and no override, the JOIN uses MAX_DAILY_SEND_CAP as the
    per-mailbox cap (the blueprint's 30–50/mailbox deliverability limit)."""
    from src.services.mailbox_dispatcher import MAX_DAILY_SEND_CAP
    session = _session_with_mailbox(client_id="client_a", daily_send_ceiling=0)
    get_active_mailbox_for_client(session, "client_a")
    # index 3 is the JOIN pick (0 lock, 1 ceiling, 2 warmed, 3 JOIN)
    assert session.execute.call_args_list[3][0][1]["cap"] == MAX_DAILY_SEND_CAP


def test_explicit_daily_send_cap_overrides_per_mailbox_cap():
    """An explicit daily_send_cap sets the per-mailbox cap in the JOIN."""
    session = _session_with_mailbox(client_id="client_a", daily_send_ceiling=0)
    get_active_mailbox_for_client(session, "client_a", daily_send_cap=17)
    assert session.execute.call_args_list[3][0][1]["cap"] == 17


def test_takes_per_client_advisory_lock_first():
    """The first execute must be the per-client advisory lock — serializing the
    ceiling check + claim so concurrent workers can't overshoot."""
    session = _session_with_mailbox(client_id="client_a")
    get_active_mailbox_for_client(session, "client_a")
    first_sql = str(session.execute.call_args_list[0][0][0]).lower()
    assert "pg_advisory_xact_lock" in first_sql


def test_zero_ceiling_means_no_aggregate_cap():
    """daily_send_ceiling = 0 → no per-client total check (no 24h count query),
    only the per-mailbox cap applies. Mailbox is still handed out."""
    session = _session_with_mailbox(client_id="client_a", daily_send_ceiling=0)
    result = get_active_mailbox_for_client(session, "client_a")
    assert result.mailbox_id == 1
    # Exactly 5 calls: lock, ceiling, warmed, JOIN, UPDATE. A per-client 24h
    # count (the 6th) is only issued when ceiling > 0.
    assert session.execute.call_count == 5


def test_client_at_ceiling_defers_before_handing_out_a_mailbox():
    """When the client's 24h total across all mailboxes has reached the ceiling,
    no mailbox is handed out — AllMailboxesCapped, counted by client_id."""
    from src.services.mailbox_dispatcher import AllMailboxesCapped
    session = MagicMock()
    session.execute.side_effect = [
        MagicMock(),        # advisory lock
        _scalar(50),        # daily_send_ceiling = 50 (per-client total)
        _scalar(50),        # client 24h sends across mailboxes = 50 → at ceiling
    ]
    with pytest.raises(AllMailboxesCapped, match="per-client daily send ceiling"):
        get_active_mailbox_for_client(session, "client_a")


def test_client_below_ceiling_proceeds_to_pick():
    """Client total below the ceiling → pick proceeds; the 24h count query is
    scoped by client_id (aggregate), not mailbox_id."""
    session = _session_with_mailbox(client_id="client_a", daily_send_ceiling=100, client_24h_count=10)
    result = get_active_mailbox_for_client(session, "client_a")
    assert result.mailbox_id == 1
    # index 2 is the client 24h count (0 lock, 1 ceiling, 2 count) — by client_id.
    count_sql = str(session.execute.call_args_list[2][0][0]).lower()
    assert "sequence_touch_dispatches" in count_sql and "client_id" in count_sql
