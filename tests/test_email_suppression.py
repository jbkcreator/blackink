"""Tests for suppression & DNC scrubbing port.

Dev 4 / Week 0 AC #3: suppression gate deterministic — no LLM, no live DB.
All tests use FakeSession (MagicMock) pattern.
"""

from unittest.mock import MagicMock, call, patch
import pytest

from src.services.email_suppression import (
    suppress_contact,
    is_suppressed,
    suppress_by_email,
    suppress_by_phone,
    suppress_by_domain,
    bulk_suppress,
    import_dnc_list,
)


# ---------------------------------------------------------------------------
# FakeSession helpers
# ---------------------------------------------------------------------------

def _row(*vals):
    m = MagicMock()
    m.__getitem__ = lambda self, i: vals[i]
    m.__iter__ = lambda self: iter(vals)
    m.__bool__ = lambda self: True
    return m


def _session_for_suppress(company_id="co_abc"):
    """Session that handles suppress_contact's two executes: UPDATE + SELECT."""
    session = MagicMock()
    calls = []

    def _exec(stmt, params):
        calls.append((str(stmt), params))
        result = MagicMock()
        sql = str(stmt).upper()
        if "SELECT" in sql and "company_id" in sql.lower():
            result.fetchone.return_value = (company_id,)
        elif "INSERT" in sql:
            result.fetchone.return_value = None
        else:
            result.fetchone.return_value = None
        result.fetchall.return_value = []
        return result

    session.execute.side_effect = _exec
    session._calls = calls
    return session


def _session_is_suppressed(opted_out: bool, suppressed: bool):
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = (opted_out, suppressed)
    session.execute.return_value = result
    return session


def _session_is_suppressed_not_found():
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = None
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# suppress_contact
# ---------------------------------------------------------------------------

def test_suppress_contact_executes_update():
    session = _session_for_suppress()
    suppress_contact(session, contact_id=5, reason="manual_opt_out")
    # First call must be the UPDATE
    first_sql = str(session.execute.call_args_list[0][0][0]).upper()
    assert "UPDATE" in first_sql
    assert "IS_OPTED_OUT" in first_sql or "is_opted_out" in str(session.execute.call_args_list[0][0][0])


def test_suppress_contact_idempotent_no_error():
    # Calling twice must not raise
    session = _session_for_suppress()
    suppress_contact(session, contact_id=5, reason="test")
    suppress_contact(session, contact_id=5, reason="test")


def test_suppress_contact_logs_event_when_company_found():
    session = _session_for_suppress(company_id="co_xyz")
    suppress_contact(session, contact_id=10, reason="dnc_import")
    calls = [str(c[0][0]) for c in session.execute.call_args_list]
    assert any("INSERT" in c.upper() for c in calls)


def test_suppress_contact_no_crash_when_company_missing():
    session = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = None
    session.execute.return_value = result
    suppress_contact(session, contact_id=99, reason="test")  # must not raise


# ---------------------------------------------------------------------------
# is_suppressed
# ---------------------------------------------------------------------------

def test_is_suppressed_true_when_opted_out():
    assert is_suppressed(_session_is_suppressed(True, False), 1) is True


def test_is_suppressed_true_when_suppression_state():
    assert is_suppressed(_session_is_suppressed(False, True), 1) is True


def test_is_suppressed_true_when_both():
    assert is_suppressed(_session_is_suppressed(True, True), 1) is True


def test_is_suppressed_false_when_neither():
    assert is_suppressed(_session_is_suppressed(False, False), 1) is False


def test_is_suppressed_false_when_not_found():
    assert is_suppressed(_session_is_suppressed_not_found(), 999) is False


# ---------------------------------------------------------------------------
# suppress_by_email
# ---------------------------------------------------------------------------

def _session_by_email(contact_ids: list[int], company_id="co_abc"):
    session = MagicMock()
    def _exec(stmt, params):
        result = MagicMock()
        sql = str(stmt).upper()
        if "WHERE LOWER(EMAIL)" in sql or "lower(email)" in str(stmt):
            result.fetchall.return_value = [(cid,) for cid in contact_ids]
        elif "COMPANY_ID" in sql:
            result.fetchone.return_value = (company_id,)
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result
    session.execute.side_effect = _exec
    return session


def test_suppress_by_email_returns_count():
    session = _session_by_email([1, 2])
    count = suppress_by_email(session, "owner@acme.com", "test")
    assert count == 2


def test_suppress_by_email_zero_when_not_found():
    session = _session_by_email([])
    count = suppress_by_email(session, "nobody@x.com", "test")
    assert count == 0


def test_suppress_by_email_lowercases():
    session = _session_by_email([])
    suppress_by_email(session, "Owner@ACME.COM", "test")
    call_args = session.execute.call_args_list[0]
    params = call_args[0][1]
    assert params["email"] == "owner@acme.com"


# ---------------------------------------------------------------------------
# suppress_by_phone
# ---------------------------------------------------------------------------

def _session_by_phone(contact_ids: list[int]):
    session = MagicMock()
    def _exec(stmt, params):
        result = MagicMock()
        sql = str(stmt).upper()
        if "WHERE PHONE" in sql or "phone" in str(params):
            result.fetchall.return_value = [(cid,) for cid in contact_ids]
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result
    session.execute.side_effect = _exec
    return session


def test_suppress_by_phone_returns_count():
    assert suppress_by_phone(_session_by_phone([7]), "+18135550100", "stop") == 1


def test_suppress_by_phone_zero_when_not_found():
    assert suppress_by_phone(_session_by_phone([]), "+19995550100", "stop") == 0


# ---------------------------------------------------------------------------
# suppress_by_domain
# ---------------------------------------------------------------------------

def _session_by_domain(contact_ids: list[int]):
    session = MagicMock()
    def _exec(stmt, params):
        result = MagicMock()
        sql = str(stmt).upper()
        if "JOIN COMPANIES" in sql or "co.domain" in str(stmt):
            result.fetchall.return_value = [(cid,) for cid in contact_ids]
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result
    session.execute.side_effect = _exec
    return session


def test_suppress_by_domain_returns_count():
    assert suppress_by_domain(_session_by_domain([3, 4]), "acme.com", "complaint") == 2


def test_suppress_by_domain_zero_when_no_contacts():
    assert suppress_by_domain(_session_by_domain([]), "ghost.com", "complaint") == 0


# ---------------------------------------------------------------------------
# bulk_suppress
# ---------------------------------------------------------------------------

def _session_bulk(contact_ids: list[int]):
    session = MagicMock()
    def _exec(stmt, params):
        result = MagicMock()
        sql = str(stmt).upper()
        if "ANY" in sql and "COMPANY_ID" in sql.upper():
            result.fetchall.return_value = [(cid, "co_abc") for cid in contact_ids]
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result
    session.execute.side_effect = _exec
    return session


def test_bulk_suppress_returns_count():
    assert bulk_suppress(_session_bulk([1, 2, 3]), [1, 2, 3], "import") == 3


def test_bulk_suppress_empty_list():
    session = MagicMock()
    assert bulk_suppress(session, [], "import") == 0
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# import_dnc_list
# ---------------------------------------------------------------------------

def test_import_dnc_list_summary():
    """Matched phones get suppressed; unmatched are logged not errored."""
    suppress_calls = []

    with patch(
        "src.services.email_suppression.suppress_by_phone",
        side_effect=lambda s, ph, reason: suppress_calls.append(ph) or (1 if ph.strip() == "+18135550100" else 0),
    ):
        session = MagicMock()
        result = import_dnc_list(session, ["+18135550100", "+19999999999"], source="national_dnc")

    assert result["matched"] == 1
    assert result["unmatched"] == 1
