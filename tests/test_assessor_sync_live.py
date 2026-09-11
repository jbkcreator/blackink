"""Live-DB tests for the county assessor sync's importer — upsert
idempotency, Hillsborough homestead transition detection, retire-on-
disappearance, failure safety (a bad import must never replace a good
roll), the row-count floor, and the actual point of the whole feature:
Win-Back's check_still_owns() returning real answers. See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md §3/§5.

Requires a real Postgres with migrations applied, pointed at by
DATABASE_URL. No skip-when-no-DB guard, matching
tests/test_tenant_isolation.py's convention.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context
from src.services.assessor.importer import ImportValidationError, import_row_owning_dataset
from src.services.assessor.mapping import map_hillsborough_row
from src.services.winback_ingest import StagingTableAssessorProvider

_PREFIX = "SYNCLIVE_"


@pytest.fixture
def db():
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id LIKE :p"), {"p": f"{_PREFIX}%"})
		session.commit()
		yield session
		session.execute(text("DELETE FROM assessor_parcels WHERE assessor_parcel_id LIKE :p"), {"p": f"{_PREFIX}%"})
		session.commit()


def _hc_row(folio: str, **overrides) -> dict:
	base = {"FOLIO": folio, "DOR_C": "0802", "OWNER": "TEST OWNER LLC", "SITE_ADDR": "1 TEST ST", "STATE": "FL", "BASE": "0"}
	base.update(overrides)
	return map_hillsborough_row(base)


def test_upsert_idempotency_reimport_yields_same_row_count(db):
	folio = f"{_PREFIX}IDEM"
	as_of = datetime.now(timezone.utc)
	for _ in range(2):
		import_row_owning_dataset(
			db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
			mapped_rows=iter([_hc_row(folio)]), as_of=as_of, owns_homestead=True, min_row_count=1,
		)
		db.commit()
	count = db.execute(
		text("SELECT COUNT(*) FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": folio}
	).scalar()
	assert count == 1


def test_hillsborough_homestead_transition_unknown_to_homestead_sets_no_previous(db):
	"""The first-ever import of a parcel must never emit a spurious
	UNKNOWN -> HOMESTEAD 'drop' signal."""
	folio = f"{_PREFIX}FIRSTIMPORT"
	as_of = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio, BASE="2015")]), as_of=as_of, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	row = db.execute(
		text("SELECT homestead_status, homestead_previous FROM assessor_parcels WHERE assessor_parcel_id = :p"),
		{"p": folio},
	).fetchone()
	assert tuple(row) == ("HOMESTEAD", None)


def test_hillsborough_homestead_transition_homestead_to_no_homestead_sets_previous(db):
	folio = f"{_PREFIX}REALDROP"
	as_of1 = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio, BASE="2015")]), as_of=as_of1, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	as_of2 = as_of1 + timedelta(seconds=1)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio, BASE="0")]), as_of=as_of2, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	row = db.execute(
		text("SELECT homestead_status, homestead_previous FROM assessor_parcels WHERE assessor_parcel_id = :p"),
		{"p": folio},
	).fetchone()
	assert tuple(row) == ("NO_HOMESTEAD", "HOMESTEAD")


def test_retire_on_disappearance_then_reappearance_clears_retired_at(db):
	folio_stays = f"{_PREFIX}STAYS"
	folio_vanishes = f"{_PREFIX}VANISHES"
	as_of1 = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio_stays), _hc_row(folio_vanishes)]),
		as_of=as_of1, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	as_of2 = as_of1 + timedelta(seconds=1)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio_stays)]), as_of=as_of2, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	retired = db.execute(
		text("SELECT retired_at IS NOT NULL FROM assessor_parcels WHERE assessor_parcel_id = :p"),
		{"p": folio_vanishes},
	).scalar()
	assert retired is True
	still_present = db.execute(
		text("SELECT retired_at IS NOT NULL FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": folio_stays}
	).scalar()
	assert still_present is False

	# Reappears next run — retired_at must clear, not stay stuck.
	as_of3 = as_of2 + timedelta(seconds=1)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio_stays), _hc_row(folio_vanishes)]),
		as_of=as_of3, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	cleared = db.execute(
		text("SELECT retired_at FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": folio_vanishes}
	).scalar()
	assert cleared is None


def test_retired_parcel_is_excluded_from_the_active_lookup_index_predicate(db):
	"""The partial index's WHERE clause (retired_at IS NULL) is what keeps
	a retired parcel from ever being a Win-Back match — assert the
	predicate directly against a retired row."""
	folio = f"{_PREFIX}RETIREDLOOKUP"
	as_of1 = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio)]), as_of=as_of1, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	as_of2 = as_of1 + timedelta(seconds=1)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([]), as_of=as_of2, owns_homestead=True, min_row_count=0,
	)
	db.commit()
	found = db.execute(
		text(
			"SELECT 1 FROM assessor_parcels WHERE assessor_parcel_id = :p "
			"AND retired_at IS NULL"
		),
		{"p": folio},
	).fetchone()
	assert found is None


def test_failed_import_leaves_prior_roll_fully_intact(db):
	"""A staged batch below the row-count floor must raise before touching
	assessor_parcels at all — the previous good roll survives untouched."""
	folio = f"{_PREFIX}SAFETY"
	as_of1 = datetime.now(timezone.utc)
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([_hc_row(folio)]), as_of=as_of1, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	before = db.execute(
		text("SELECT owner_name_on_roll FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": folio}
	).scalar()

	with pytest.raises(ImportValidationError):
		import_row_owning_dataset(
			db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
			mapped_rows=iter([]), as_of=as_of1 + timedelta(seconds=1), owns_homestead=True,
			# default min_row_count (10) — an empty staged batch trips it.
		)
	db.rollback()

	after = db.execute(
		text("SELECT owner_name_on_roll FROM assessor_parcels WHERE assessor_parcel_id = :p"), {"p": folio}
	).scalar()
	assert after == before


def test_row_count_floor_rejects_a_batch_far_smaller_than_the_last_success(db):
	folio = f"{_PREFIX}FLOOR"
	as_of1 = datetime.now(timezone.utc)
	# 20 rows for this dataset — set a real "last successful" count.
	rows = [_hc_row(f"{_PREFIX}FLOOR{i}") for i in range(20)]
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name=f"{folio}_DATASET",
		mapped_rows=iter(rows), as_of=as_of1, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	db.execute(
		text(
			"INSERT INTO assessor_sync_state (county_slug, dataset_name, row_count) "
			"VALUES ('hillsborough_fl', :ds, 20) "
			"ON CONFLICT (county_slug, dataset_name) DO UPDATE SET row_count = 20"
		),
		{"ds": f"{folio}_DATASET"},
	)
	db.commit()

	# A new batch of only 3 rows (< 50% of 20) must be rejected.
	with pytest.raises(ImportValidationError, match="likely a truncated"):
		import_row_owning_dataset(
			db, county_slug="hillsborough_fl", dataset_name=f"{folio}_DATASET",
			mapped_rows=iter([_hc_row(f"{_PREFIX}FLOORNEW{i}") for i in range(3)]),
			as_of=as_of1 + timedelta(seconds=1), owns_homestead=True, min_row_count=1,
		)
	db.rollback()
	db.execute(text("DELETE FROM assessor_sync_state WHERE dataset_name = :ds"), {"ds": f"{folio}_DATASET"})
	db.commit()


def test_check_still_owns_returns_real_answers_not_always_none(db):
	"""The actual point of the whole feature: Win-Back's assessor lookup
	must return a real True/False/None, not the pre-sync unconditional
	None."""
	folio = f"{_PREFIX}WINBACK"
	as_of = datetime.now(timezone.utc)
	row = _hc_row(folio, OWNER="JOHN SMITH", SITE_ADDR="42 REAL ADDRESS LN")
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([row]), as_of=as_of, owns_homestead=True, min_row_count=1,
	)
	db.commit()

	provider = StagingTableAssessorProvider(db)
	assert provider.check_still_owns("hillsborough_fl", "42 Real Address Ln", "John Smith") is True
	assert provider.check_still_owns("hillsborough_fl", "42 Real Address Ln", "Completely Different Person") is False
	assert provider.check_still_owns("hillsborough_fl", "99 Nonexistent Rd", "John Smith") is None


def test_check_still_owns_never_matches_an_ineligible_parcel(db):
	"""A government-owned parcel was never a Win-Back target — the
	provider's query filters on is_blackink_eligible, not just address."""
	folio = f"{_PREFIX}GOVWINBACK"
	as_of = datetime.now(timezone.utc)
	row = _hc_row(folio, DOR_C="8600", OWNER="HILLSBOROUGH COUNTY", SITE_ADDR="1 GOV PLAZA")
	import_row_owning_dataset(
		db, county_slug="hillsborough_fl", dataset_name="PARCEL_SPREADSHEET",
		mapped_rows=iter([row]), as_of=as_of, owns_homestead=True, min_row_count=1,
	)
	db.commit()
	provider = StagingTableAssessorProvider(db)
	assert provider.check_still_owns("hillsborough_fl", "1 Gov Plaza", "Hillsborough County") is None


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-v"]))
