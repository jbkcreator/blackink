"""Live-DB regression coverage for the write-ownership bug found in review:
two independently-scheduled Pinellas files (RP_PROPERTY_INFO, RP_EXEMPTIONS)
upsert the same assessor_parcels rows. See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §2.5.

Requires a real Postgres with migrations 1-N applied, including
apply_assessor_sync.py, pointed at by DATABASE_URL. No skip-when-no-DB
guard, matching this repo's tests/test_tenant_isolation.py convention —
these tests simply fail if the DB isn't reachable.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context
from src.services.assessor.importer import import_pinellas_exemptions, import_row_owning_dataset
from src.services.assessor.mapping import map_pinellas_property_info_row

_TEST_PREFIX = "COTEST_"


@pytest.fixture
def db():
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id LIKE :p"), {"p": f"{_TEST_PREFIX}%"})
		session.commit()
		yield session
		session.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id LIKE :p"), {"p": f"{_TEST_PREFIX}%"})
		session.commit()


def _pinellas_row(strap: str, roll_year: str = "2026") -> dict:
	return map_pinellas_property_info_row(
		{"STRAP": strap, "PROPERTY_USE": "0110", "OWNER1": "TEST OWNER", "SITE_ADDRESS": "1 X ST", "ROLL_YEAR": roll_year}
	)


def test_exemptions_update_on_unmatched_strap_is_a_silent_no_op_never_an_error(db):
	"""No parcel exists yet for this STRAP — RP_EXEMPTIONS's pure UPDATE
	must not error and must not create one (it has no address/owner data,
	so an INSERT would violate NOT NULL constraints)."""
	as_of = datetime.now(timezone.utc)
	result = import_pinellas_exemptions(
		db,
		raw_rows=iter([{"STRAP": f"{_TEST_PREFIX}NOMATCH", "HX_YR": "2026", "HX_YN": "Yes",
		                "HX_USE": "100%", "HX_STATUS": "", "PROPERTY_EXEMPTION": ""}]),
		as_of=as_of,
		min_row_count=1,
	)
	assert result.rows_upserted == 0
	row = db.execute(
		text("SELECT 1 FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": f"{_TEST_PREFIX}NOMATCH"}
	).fetchone()
	assert row is None


def test_property_info_reimport_never_reverts_exemptions_owned_columns(db):
	"""A re-run of RP_PROPERTY_INFO's own upsert must not revert homestead
	data that only RP_EXEMPTIONS knows about — the column-ownership
	partition's whole point."""
	strap = f"{_TEST_PREFIX}REVERT"
	as_of1 = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="pinellas_fl", dataset_name="RP_PROPERTY_INFO",
		mapped_rows=iter([_pinellas_row(strap)]), as_of=as_of1, owns_homestead=False, min_row_count=1,
	)
	db.commit()
	import_pinellas_exemptions(
		db,
		raw_rows=iter([{"STRAP": strap, "HX_YR": "2026", "HX_YN": "Yes", "HX_USE": "100%",
		                "HX_STATUS": "", "PROPERTY_EXEMPTION": ""}]),
		as_of=as_of1,
		min_row_count=1,
	)
	db.commit()
	row = db.execute(
		text("SELECT homestead_status, exemption_excluded FROM assessor_parcels WHERE assessor_parcel_id = :p"),
		{"p": strap},
	).fetchone()
	assert tuple(row) == ("HOMESTEAD", False)

	# Re-import RP_PROPERTY_INFO — must not touch homestead_status/exemption_excluded.
	as_of2 = as_of1 + timedelta(seconds=1)
	import_row_owning_dataset(
		db, county_slug="pinellas_fl", dataset_name="RP_PROPERTY_INFO",
		mapped_rows=iter([_pinellas_row(strap)]), as_of=as_of2, owns_homestead=False, min_row_count=1,
	)
	db.commit()
	row2 = db.execute(
		text("SELECT homestead_status, exemption_excluded FROM assessor_parcels WHERE assessor_parcel_id = :p"),
		{"p": strap},
	).fetchone()
	assert tuple(row2) == ("HOMESTEAD", False)


def test_exemptions_update_never_touches_source_dataset_or_last_seen_at(db):
	"""If RP_EXEMPTIONS's update touched these, the NEXT RP_PROPERTY_INFO
	retire pass would stop seeing the row and wrongly retire a parcel
	still present on the roll — the exact bug this design fixes."""
	strap = f"{_TEST_PREFIX}RETIRE"
	as_of1 = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="pinellas_fl", dataset_name="RP_PROPERTY_INFO",
		mapped_rows=iter([_pinellas_row(strap)]), as_of=as_of1, owns_homestead=False, min_row_count=1,
	)
	db.commit()
	before = db.execute(
		text("SELECT source_dataset, last_seen_at FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": strap}
	).fetchone()

	as_of2 = as_of1 + timedelta(seconds=1)
	import_pinellas_exemptions(
		db,
		raw_rows=iter([{"STRAP": strap, "HX_YR": "2026", "HX_YN": "No", "HX_USE": "0%",
		                "HX_STATUS": "", "PROPERTY_EXEMPTION": ""}]),
		as_of=as_of2,
		min_row_count=1,
	)
	db.commit()
	after = db.execute(
		text("SELECT source_dataset, last_seen_at FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": strap}
	).fetchone()
	assert tuple(before) == tuple(after)

	# A subsequent RP_PROPERTY_INFO retire pass at as_of2 must NOT retire
	# this parcel — last_seen_at is still as_of1 (< as_of2 would normally
	# mean "vanished from the roll"), but since it WAS in this run's
	# staged set it gets refreshed instead of retired. Prove that by
	# re-importing at as_of2 and confirming it stays un-retired.
	import_row_owning_dataset(
		db, county_slug="pinellas_fl", dataset_name="RP_PROPERTY_INFO",
		mapped_rows=iter([_pinellas_row(strap)]), as_of=as_of2, owns_homestead=False, min_row_count=1,
	)
	db.commit()
	retired = db.execute(
		text("SELECT retired_at FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": strap}
	).fetchone()
	assert retired[0] is None


def test_exemptions_import_never_runs_a_retire_pass(db):
	"""RP_EXEMPTIONS owns no row-lifecycle columns and must never retire
	anything, even a parcel it has no data for."""
	strap = f"{_TEST_PREFIX}NORETIRE"
	as_of = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="pinellas_fl", dataset_name="RP_PROPERTY_INFO",
		mapped_rows=iter([_pinellas_row(strap)]), as_of=as_of, owns_homestead=False, min_row_count=1,
	)
	db.commit()
	import_pinellas_exemptions(
		db, raw_rows=iter([{"STRAP": f"{_TEST_PREFIX}OTHER", "HX_YR": "2026", "HX_YN": "Yes",
		                    "HX_USE": "100%", "HX_STATUS": "", "PROPERTY_EXEMPTION": ""}]),
		as_of=as_of + timedelta(days=365),  # far in the future — would trip a retire pass if one ran
		min_row_count=1,
	)
	db.commit()
	retired = db.execute(
		text("SELECT retired_at FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": strap}
	).fetchone()
	assert retired[0] is None


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-v"]))
