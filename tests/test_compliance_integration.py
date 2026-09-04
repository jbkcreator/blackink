"""Live-Postgres integration tests for the compliance suppression path.

These guard against a class of bug that MagicMock-based unit tests cannot
catch: real NOT NULL / CHECK constraints and forced Row-Level Security. Each
test below maps to a real defect that shipped past the mock tests and was
fixed:

  1. Suppression must survive commit. events.client_id is NOT NULL; an earlier
     version inserted a suppression event with NULL client_id, which aborted
     the transaction and silently rolled back the contact opt-out.
  2. suppress_by_phone must match E.164-stored numbers. contacts.phone carries
     a CHECK (^\\+[1-9]\\d{1,14}$); an earlier query normalized only the input,
     not the stored value, so +18135550100 matched nothing.
  3. dnc_refresh._collect_contacts must run under a BYPASSRLS system session.
     contacts has forced RLS; a bare app session with no client_id sees zero
     rows, making the monthly refresh a silent no-op.

Requires a real Postgres with migrations 1-11 applied, pointed at by
DATABASE_URL_APP / DATABASE_URL_SYSTEM. Skips (not fails) when no live DB is
reachable, mirroring the gating intent of tests/test_tenant_isolation.py.

Never run against a database holding real prospect data — CI / isolated
test-DB only. All rows created here use a distinguishing domain suffix and are
torn down in the fixture.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from src.core.database import get_db_context, get_system_db_context
from src.loaders.base import BaseIngestLoader
from src.services.email_suppression import (
    suppress_by_phone,
    suppress_contact,
)
from src.tasks import dnc_refresh

_CLIENT_ID = "_compcanary"
_COUNTY = "hillsborough_fl"
_DOMAIN_SUFFIX = ".compcanary.test"


def _live_db_reachable() -> bool:
    try:
        with get_system_db_context() as session:
            session.execute(text("SELECT 1"))
        return True
    except (OperationalError, SQLAlchemyError, RuntimeError):
        return False


pytestmark = pytest.mark.skipif(
    not _live_db_reachable(),
    reason="No live Postgres reachable via DATABASE_URL_SYSTEM — integration test skipped.",
)


@pytest.fixture
def canary_contact():
    """Seed one client + company + contact with a phone, tear down after.

    Uses the BYPASSRLS system session because it must create tenant rows and
    read them back across RLS. Yields a dict with the created ids.
    """
    domain = f"{_CLIENT_ID}{_DOMAIN_SUFFIX}"
    company_id = BaseIngestLoader.compute_company_id(domain)

    with get_system_db_context() as session:
        session.execute(
            text(
                "INSERT INTO clients (client_id, display_name, is_active) "
                "VALUES (:cid, :cid, TRUE) ON CONFLICT (client_id) DO NOTHING"
            ),
            {"cid": _CLIENT_ID},
        )
        session.execute(
            text(
                "INSERT INTO companies "
                "(company_id, company_name, domain, county_slug, owning_client_id) "
                "VALUES (:company_id, :name, :domain, :county, :cid) "
                "ON CONFLICT (company_id) DO NOTHING"
            ),
            {
                "company_id": company_id,
                "name": "Canary Compliance Co",
                "domain": domain,
                "county": _COUNTY,
                "cid": _CLIENT_ID,
            },
        )
        contact_id = session.execute(
            text(
                "INSERT INTO contacts "
                "(company_id, contact_role_type, email, phone, phone_type) "
                "VALUES (:company_id, 'OWNER_BROKER_MD', :email, :phone, 'MOBILE') "
                "RETURNING contact_id"
            ),
            {
                "company_id": company_id,
                "email": f"owner@{domain}",
                "phone": "+18135550100",
            },
        ).scalar()

    yield {"company_id": company_id, "contact_id": contact_id, "domain": domain}

    with get_system_db_context() as session:
        session.execute(text("DELETE FROM contacts WHERE company_id = :cid"), {"cid": company_id})
        session.execute(text("DELETE FROM companies WHERE company_id = :cid"), {"cid": company_id})
        session.execute(text("DELETE FROM clients WHERE client_id = :cid"), {"cid": _CLIENT_ID})


# ---------------------------------------------------------------------------
# 1. Suppression survives commit (no events-ledger NULL-insert tx abort)
# ---------------------------------------------------------------------------

def test_suppress_contact_survives_commit(canary_contact):
    contact_id = canary_contact["contact_id"]

    with get_system_db_context() as session:
        suppress_contact(session, contact_id, "test")
        # commit happens on context exit — must NOT be aborted by any side write

    # Re-read in a brand-new session: the opt-out must have persisted.
    with get_system_db_context() as session:
        row = session.execute(
            text(
                "SELECT is_opted_out, suppression_state "
                "FROM contacts WHERE contact_id = :cid"
            ),
            {"cid": contact_id},
        ).fetchone()

    assert row is not None, "contact vanished after suppression commit"
    assert row.is_opted_out is True, "is_opted_out did not persist — transaction was rolled back"
    assert row.suppression_state is True, "suppression_state did not persist"


# ---------------------------------------------------------------------------
# 2. E.164 phone match
# ---------------------------------------------------------------------------

def test_suppress_by_phone_matches_e164(canary_contact):
    contact_id = canary_contact["contact_id"]

    with get_system_db_context() as session:
        # Stored as +18135550100; caller passes the 10-digit form.
        count = suppress_by_phone(session, "8135550100", "test")

    assert count == 1, "suppress_by_phone did not match the E.164-stored number"

    with get_system_db_context() as session:
        row = session.execute(
            text("SELECT is_opted_out FROM contacts WHERE contact_id = :cid"),
            {"cid": contact_id},
        ).fetchone()
    assert row.is_opted_out is True, "E.164 contact was not suppressed"


# ---------------------------------------------------------------------------
# 3. DNC refresh collection under forced RLS
# ---------------------------------------------------------------------------

def test_dnc_collect_sees_rows_under_system_session(canary_contact):
    contact_id = canary_contact["contact_id"]
    cutoff = datetime.now(timezone.utc)

    with get_system_db_context() as session:
        collected = dnc_refresh._collect_contacts(session, cutoff, limit=None)

    ids = {cid for cid, _phone in collected}
    assert contact_id in ids, (
        "system (BYPASSRLS) session did not collect the eligible contact — "
        "dnc_refresh would be a silent no-op"
    )


def test_dnc_collect_bare_app_session_sees_nothing(canary_contact):
    """A bare app session with no client_id must see zero contacts under forced
    RLS — this is exactly the silent no-op the system-session fix avoids."""
    cutoff = datetime.now(timezone.utc)

    with get_db_context() as session:  # no client_id → RLS backstop
        collected = dnc_refresh._collect_contacts(session, cutoff, limit=None)

    ids = {cid for cid, _phone in collected}
    assert canary_contact["contact_id"] not in ids, (
        "bare app session saw a tenant contact — RLS backstop is not holding"
    )
