"""End-to-end sweep test for the unprocessable-address PR review finding
(confirmed real): src/services/owner_enrichment.py's TracerfyEnrichmentProvider
excludes an address it can't parse from submission entirely -- before this
fix, that made an unparseable-address row indistinguishable from a genuine
"vendor found nothing" miss once it reached apply_result(), so a row that
already had a pre-existing CSV email got requires_enrichment_review=FALSE
via apply_result's has_usable_email fallback despite Tracerfy never having
been asked about it -- letting it pass the /arm gate
(enrichment_timestamp IS NOT NULL AND requires_enrichment_review = FALSE)
unenriched.

Requires a real Postgres instance with migrations 1-N applied, same posture
as tests/test_enrichment_verification_concurrency.py and
tests/test_tenant_isolation.py.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_owner_db_context, get_system_db_context
from src.services.owner_enrichment import TracerfyEnrichmentProvider
from src.tasks.enrichment_verification import _claim_rows, _enrich_claimed_rows, _mark_claimed
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401


@pytest.fixture
def unparseable_address_row_with_existing_email(canary_tenants):  # noqa: F811
    """One winback_rows row with an address _split_address cannot parse (no
    "STREET, CITY, ST" comma structure) but a pre-existing email -- the
    exact shape the PR review flagged. Teardown via get_owner_db_context(),
    same reason as the concurrency test's own fixture: blackink_system has
    no DELETE grant on winback_imports/winback_rows."""
    import_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with get_system_db_context() as session:
        session.execute(
            text(
                "INSERT INTO winback_imports (import_id, client_id, filename, uploaded_by, status, created_at) "
                "VALUES (:iid, :cid, 'unprocessable_address_test.csv', 'pytest', 'COMPLETED', :now)"
            ),
            {"iid": import_id, "cid": CANARY_A, "now": now},
        )
        row_id = session.execute(
            text(
                "INSERT INTO winback_rows (import_id, client_id, owner_name, property_address_raw, "
                "property_address_normalized, county_slug, phone, email, disposition, "
                "requires_human_review, suppression_state, created_at, updated_at) "
                "VALUES (:iid, :cid, 'Unparseable Address Owner', '1 Unparseable Rd', '1 UNPARSEABLE RD', "
                "NULL, NULL, 'preexisting@example.com', 'STILL_OWNS_STILL_RENTING', FALSE, FALSE, :now, :now) "
                "RETURNING winback_row_id"
            ),
            {"iid": import_id, "cid": CANARY_A, "now": now},
        ).scalar_one()
        session.commit()

    yield import_id, row_id

    with get_owner_db_context() as session:
        session.execute(text("DELETE FROM winback_rows WHERE winback_row_id = :id"), {"id": row_id})
        session.execute(text("DELETE FROM winback_imports WHERE import_id = :iid"), {"iid": import_id})
        # _enrich_claimed_rows logs an owner_enrichment_completed event for
        # CANARY_A -- must go before canary_tenants' own teardown (which
        # runs after this fixture's, per pytest's LIFO fixture-teardown
        # order) or its DELETE FROM clients hits events_client_id_fkey.
        session.execute(text("DELETE FROM events WHERE client_id = :cid"), {"cid": CANARY_A})
        session.commit()


def test_unparseable_address_row_with_preexisting_email_stays_unarmable(unparseable_address_row_with_existing_email):
    import_id, row_id = unparseable_address_row_with_existing_email
    now = datetime.now(timezone.utc)
    settings = get_settings()

    with get_system_db_context() as session:
        rows = _claim_rows(session, CANARY_A, import_id, max_attempts=3, limit=10, now=now)
        assert row_id in [r.winback_row_id for r in rows], "fixture row must be claimable"
        _mark_claimed(session, [r.winback_row_id for r in rows], now)
        session.commit()

        counts = {"enriched": 0, "failed": 0, "submit_failures": 0}
        _enrich_claimed_rows(session, CANARY_A, rows, TracerfyEnrichmentProvider("fake-key"), settings, counts)

        # requires_enrichment_review must be forced TRUE despite the row's
        # pre-existing email -- the bug this test guards against.
        after = session.execute(
            text(
                "SELECT enrichment_timestamp, requires_enrichment_review, email "
                "FROM winback_rows WHERE winback_row_id = :id"
            ),
            {"id": row_id},
        ).fetchone()
        assert after.enrichment_timestamp is not None
        assert after.requires_enrichment_review is True
        assert after.email == "preexisting@example.com"  # never erased

        # The exact /arm gate query from src/api/winback_router.py --
        # this row must be absent from its result.
        armable = session.execute(
            text(
                "SELECT winback_row_id FROM winback_rows WHERE import_id = :import_id AND client_id = :client_id "
                "AND disposition IN ('STILL_OWNS_STILL_RENTING', 'STILL_OWNS_NOT_RENTING') "
                "AND suppression_state = FALSE AND stopped_at IS NULL "
                "AND enrichment_timestamp IS NOT NULL AND requires_enrichment_review = FALSE"
            ),
            {"import_id": import_id, "client_id": CANARY_A},
        ).fetchall()
        assert row_id not in [r.winback_row_id for r in armable], (
            "an unparseable-address row must never pass the /arm gate, even with a pre-existing email"
        )
