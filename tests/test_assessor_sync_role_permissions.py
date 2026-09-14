"""Verifies the assessor sync pipeline actually works under the REAL
production role (blackink_system / get_system_db_context) — not the owner
role every other test in this suite uses for convenience.

This distinction matters and is not merely stylistic: every other
test_assessor_*.py file uses get_owner_db_context(), which implicitly has
every privilege (it's the migration-running role). Testing exclusively
under that role can silently hide a real permission gap that would only
surface in actual production, since importer.py/assessor_sync.py's real
code path always runs as blackink_system (BYPASSRLS, but NOT
privilege-unrestricted — see apply_assessor_sync.py's GRANT/REVOKE list).

Found in review (2026-09-11): this database carries a schema-wide default
privilege (undocumented anywhere in this repo's own migrations) that
auto-grants blackink_system ALL privileges on any newly-created table at
CREATE TABLE time — a plain narrower GRANT in the migration does not
override it; only an explicit REVOKE does. Confirmed by hand before this
test existed; this test is what keeps that finding from silently
regressing if the migration is ever edited without noticing.

Requires a real Postgres with migrations applied (DATABASE_URL /
DATABASE_URL_SYSTEM), matching this repo's live-DB test convention — no
skip-when-no-DB guard.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.services.assessor.importer import ImportResult, import_row_owning_dataset
from src.services.assessor.mapping import map_hillsborough_row
from src.tasks.assessor_sync import _claim, _mark

_PREFIX = "ROLEPERM_"


@pytest.fixture
def cleanup():
	"""Setup/teardown uses the owner role deliberately — test scaffolding
	is allowed broader privileges than the code under test; that's normal
	and distinct from what this file actually verifies."""
	with get_owner_db_context() as db:
		db.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id LIKE :p"), {"p": f"{_PREFIX}%"})
		db.commit()
	yield
	with get_owner_db_context() as db:
		db.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id LIKE :p"), {"p": f"{_PREFIX}%"})
		db.execute(
			text(
				"UPDATE assessor_sync_state SET import_status='PENDING', attempts=0, "
				"next_retry_at=NULL, claimed_at=NULL, last_error=NULL, failure_category=NULL, "
				"row_count=NULL, source_published_at=NULL"
			)
		)
		db.commit()


def test_import_row_owning_dataset_works_under_blackink_system(cleanup):
	"""The real production role must be able to: create+COPY into a temp
	table, INSERT a brand-new parcel (calling nextval() on the parcel
	sequence), UPDATE via ON CONFLICT, and run the retire query — none of
	which is proven by any other test in this suite, all of which use the
	owner role instead."""
	as_of = datetime.now(timezone.utc)
	row = map_hillsborough_row(
		{"FOLIO": f"{_PREFIX}1", "DOR_C": "0802", "OWNER": "ABC RENTALS LLC", "SITE_ADDR": "1 TEST ST", "STATE": "FL", "BASE": "0"}
	)
	with get_system_db_context() as db:
		result = import_row_owning_dataset(
			db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
			mapped_rows=iter([row]), as_of=as_of, owns_homestead=True, min_row_count=1,
		)
		db.commit()
	assert result == ImportResult(rows_staged=1, rows_upserted=1, rows_retired=0)

	with get_owner_db_context() as db:
		eligible = db.execute(
			text("SELECT is_blackink_eligible FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": f"{_PREFIX}1"}
		).scalar()
	assert eligible is True


def test_claim_and_mark_work_under_blackink_system(cleanup):
	"""_claim/_mark only ever UPDATE assessor_sync_state — verifies that
	still works after the review-driven REVOKE INSERT/DELETE on this
	table (the DELETE/INSERT removal must not have collaterally broken
	UPDATE, which the real sweep depends on for every run)."""
	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as db:
		assert _claim(db, "hillsborough_fl", "PARCEL_SPREADSHEET", as_of) is True
		status = _mark(
			db, "hillsborough_fl", "PARCEL_SPREADSHEET", status="SUCCESS",
			result=ImportResult(rows_staged=1, rows_upserted=1), as_of=as_of,
		)
	assert status is None  # SUCCESS marks return None; only FAILED marks return a status


def test_blackink_system_cannot_insert_or_delete_assessor_sync_state():
	"""Positive confirmation of the tightened grant itself — assessor_sync_state's
	3 rows are seeded once by the migration and only ever UPDATEd; blackink_system
	must NOT be able to insert a new row or delete an existing one."""
	with get_system_db_context() as db:
		with pytest.raises(Exception, match="permission denied"):
			db.execute(
				text("INSERT INTO assessor_sync_state (county_slug, dataset_name) VALUES ('pinellas_fl', 'SHOULD_FAIL')")
			)
		db.rollback()
		with pytest.raises(Exception, match="permission denied"):
			db.execute(text("DELETE FROM assessor_sync_state WHERE dataset_name = 'RP_EXEMPTIONS'"))
		db.rollback()


def test_blackink_system_cannot_delete_assessor_parcels():
	"""Positive confirmation: a parcel is soft-retired (retired_at), never
	row-deleted at runtime — blackink_system must not be able to delete."""
	with get_system_db_context() as db:
		with pytest.raises(Exception, match="permission denied"):
			db.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id = 'NONEXISTENT'"))
		db.rollback()


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-v"]))
