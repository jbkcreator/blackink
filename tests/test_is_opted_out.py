"""Unit tests for src/services/slack/listeners.py::_is_opted_out — the
fail-closed opt-out check shared by Reply in Thread, Book Meeting, and
Approve KB Draft (S-10/S-11).

Code-review fix: the prior version returned False (not blocked)
unconditionally whenever contact_id was None — a real gap for an
"unattributed" reply (contact_id legitimately NULL when ingestion
attribution fails). This suite proves the corrected behavior: contact_id
lookup when available, a tenant-scoped normalized-email fallback, a
tenant-scoped domain-suppression fallback (excluding public email
providers), and fail-closed when neither identity is usable at all.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.services.slack.listeners import _is_opted_out


def _fake_session(*, by_contact_id=None, by_email_and_client=None, domain_suppressed=None):
    """by_contact_id: {contact_id: bool}
    by_email_and_client: {(client_id, normalized_email): bool}
    domain_suppressed: set of (client_id, domain) tuples with a suppressed contact
    """
    by_contact_id = by_contact_id or {}
    by_email_and_client = by_email_and_client or {}
    domain_suppressed = domain_suppressed or set()

    session = MagicMock()

    def _execute(stmt, params=None, *a, **kw):
        sql = str(stmt)
        result = MagicMock()
        params = params or {}
        if "WHERE contact_id = :cid" in sql:
            if params["cid"] in by_contact_id:
                result.first.return_value = SimpleNamespace(is_opted_out=by_contact_id[params["cid"]])
            else:
                result.first.return_value = None
        elif "lower(c.email) = :email" in sql:
            key = (params["client_id"], params["email"])
            if key in by_email_and_client:
                result.first.return_value = SimpleNamespace(is_opted_out=by_email_and_client[key])
            else:
                result.first.return_value = None
        elif "c.is_opted_out = TRUE" in sql:
            key = (params["client_id"], params["domain"])
            result.first.return_value = SimpleNamespace() if key in domain_suppressed else None
        else:
            result.first.return_value = None
        return result

    session.execute.side_effect = _execute
    return session


# ---------------------------------------------------------------------------
# contact_id known -- unchanged behavior
# ---------------------------------------------------------------------------

def test_contact_id_known_and_opted_out_is_blocked():
    session = _fake_session(by_contact_id={42: True})
    assert _is_opted_out(session, "CL_A", 42, "someone@acme.com") is True


def test_contact_id_known_and_not_opted_out_is_permitted():
    session = _fake_session(by_contact_id={42: False})
    assert _is_opted_out(session, "CL_A", 42, "someone@acme.com") is False


# ---------------------------------------------------------------------------
# Required scenario 1: contact_id NULL + exact suppressed email -> blocked
# ---------------------------------------------------------------------------

def test_contact_id_null_exact_suppressed_email_is_blocked():
    session = _fake_session(by_email_and_client={("CL_A", "owner@acme.com"): True})
    assert _is_opted_out(session, "CL_A", None, "owner@acme.com") is True


def test_email_normalization_case_and_whitespace():
    session = _fake_session(by_email_and_client={("CL_A", "owner@acme.com"): True})
    assert _is_opted_out(session, "CL_A", None, "  Owner@ACME.com  ") is True


# ---------------------------------------------------------------------------
# Required scenario 2: contact_id NULL + suppressed business domain -> blocked
# ---------------------------------------------------------------------------

def test_contact_id_null_suppressed_business_domain_is_blocked():
    """No contact row exists yet for THIS sender, but another contact at
    the same business domain has already been suppressed via
    suppress_by_domain()."""
    session = _fake_session(domain_suppressed={("CL_A", "acme.com")})
    assert _is_opted_out(session, "CL_A", None, "new-person@acme.com") is True


def test_public_email_domain_never_treated_as_company_wide_suppression():
    """Even if (hypothetically) some contact at gmail.com is suppressed,
    that must never block an unrelated sender who also happens to use
    gmail.com -- public/free providers are excluded from the domain check
    entirely."""
    session = _fake_session(domain_suppressed={("CL_A", "gmail.com")})
    assert _is_opted_out(session, "CL_A", None, "unrelated.person@gmail.com") is False


# ---------------------------------------------------------------------------
# Required scenario 3: same email/domain under a DIFFERENT tenant -> not blocked
# ---------------------------------------------------------------------------

def test_same_email_suppressed_under_a_different_tenant_is_not_blocked():
    session = _fake_session(by_email_and_client={("CL_B", "owner@acme.com"): True})
    assert _is_opted_out(session, "CL_A", None, "owner@acme.com") is False


def test_same_domain_suppressed_under_a_different_tenant_is_not_blocked():
    session = _fake_session(domain_suppressed={("CL_B", "acme.com")})
    assert _is_opted_out(session, "CL_A", None, "new-person@acme.com") is False


# ---------------------------------------------------------------------------
# Required scenario 4: unsuppressed sender -> permitted
# ---------------------------------------------------------------------------

def test_genuinely_new_unsuppressed_sender_is_permitted():
    session = _fake_session()  # no rows anywhere
    assert _is_opted_out(session, "CL_A", None, "brand-new@acme.com") is False


# ---------------------------------------------------------------------------
# Required scenario 5: missing contact_id AND missing sender_email -> fail closed
# ---------------------------------------------------------------------------

def test_missing_contact_id_and_missing_sender_email_fails_closed():
    session = _fake_session()
    assert _is_opted_out(session, "CL_A", None, None) is True
    assert _is_opted_out(session, "CL_A", None, "") is True
    assert _is_opted_out(session, "CL_A", None, "   ") is True


def test_malformed_email_with_no_at_sign_fails_closed():
    session = _fake_session()
    assert _is_opted_out(session, "CL_A", None, "not-an-email") is True


# ---------------------------------------------------------------------------
# Tenant-scoping mechanics: never queries without client_id bound in
# ---------------------------------------------------------------------------

def test_email_lookup_query_is_bound_to_the_given_client_id():
    session = _fake_session()
    _is_opted_out(session, "CL_A", None, "someone@acme.com")
    calls = [c for c in session.execute.call_args_list if "lower(c.email)" in str(c[0][0])]
    assert len(calls) == 1
    assert calls[0][0][1]["client_id"] == "CL_A"


def test_domain_lookup_query_is_bound_to_the_given_client_id():
    session = _fake_session()
    _is_opted_out(session, "CL_A", None, "someone@acme.com")
    calls = [c for c in session.execute.call_args_list if "c.is_opted_out = TRUE" in str(c[0][0])]
    assert len(calls) == 1
    assert calls[0][0][1]["client_id"] == "CL_A"
