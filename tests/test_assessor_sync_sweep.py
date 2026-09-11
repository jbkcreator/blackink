"""Live-DB tests for src/tasks/assessor_sync.py's own bookkeeping logic
(_mark, _claim) — not the download/import pipeline itself (covered by
tests/test_assessor_sync_live.py). No network calls here. Requires a real
Postgres, pointed at by DATABASE_URL, matching this repo's
tests/test_tenant_isolation.py convention (no skip-when-no-DB guard).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context
from src.tasks.assessor_sync import _claim, _mark

_TEST_DATASET = "SWEEPTEST_DATASET"


@pytest.fixture
def db():
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM assessor_sync_state WHERE dataset_name = :d"), {"d": _TEST_DATASET})
		session.execute(
			text("INSERT INTO assessor_sync_state (county_slug, dataset_name) VALUES ('pinellas_fl', :d)"),
			{"d": _TEST_DATASET},
		)
		session.commit()
		yield session
		session.execute(text("DELETE FROM assessor_sync_state WHERE dataset_name = :d"), {"d": _TEST_DATASET})
		session.commit()


def test_mark_returns_the_status_it_actually_persisted_not_a_hardcoded_guess(db):
	"""Regression test for a bug found in review: every call site used to
	hardcode the literal string "FAILED" in its DatasetSyncResult,
	ignoring that _mark internally may have escalated to
	FAILED_PERMANENT based on attempt count — silently making
	run_sweep's "N datasets permanently failed" alert unreachable."""
	as_of = datetime.now(timezone.utc)
	returned = _mark(db, "pinellas_fl", _TEST_DATASET, status="FAILED", error="boom", failure_category="INTERNAL_ERROR", as_of=as_of)
	db.commit()
	persisted = db.execute(
		text("SELECT import_status FROM assessor_sync_state WHERE dataset_name = :d"), {"d": _TEST_DATASET}
	).scalar()
	assert returned == persisted == "FAILED"


def test_mark_escalates_to_failed_permanent_after_max_attempts_and_returns_it(db):
	as_of = datetime.now(timezone.utc)
	db.execute(text("UPDATE assessor_sync_state SET attempts = 2 WHERE dataset_name = :d"), {"d": _TEST_DATASET})
	db.commit()
	# Default ASSESSOR_SYNC_MAX_ATTEMPTS is 3 — this is the 3rd attempt.
	returned = _mark(db, "pinellas_fl", _TEST_DATASET, status="FAILED", error="boom", failure_category="INTERNAL_ERROR", as_of=as_of)
	db.commit()
	persisted = db.execute(
		text("SELECT import_status, attempts, next_retry_at FROM assessor_sync_state WHERE dataset_name = :d"),
		{"d": _TEST_DATASET},
	).fetchone()
	assert returned == "FAILED_PERMANENT"
	assert persisted.import_status == "FAILED_PERMANENT"
	assert persisted.attempts == 3
	assert persisted.next_retry_at is None  # a permanent failure is never retried


def test_mark_success_resets_attempts_and_clears_failure_state(db):
	as_of = datetime.now(timezone.utc)
	db.execute(
		text("UPDATE assessor_sync_state SET attempts = 2, failure_category = 'NETWORK_TIMEOUT', last_error = 'x' WHERE dataset_name = :d"),
		{"d": _TEST_DATASET},
	)
	db.commit()
	from src.services.assessor.importer import ImportResult

	returned = _mark(
		db, "pinellas_fl", _TEST_DATASET, status="SUCCESS", result=ImportResult(rows_staged=100, rows_upserted=100), as_of=as_of,
	)
	db.commit()
	assert returned is None  # only FAILED marks return a status
	row = db.execute(
		text("SELECT import_status, attempts, failure_category, last_error, row_count FROM assessor_sync_state WHERE dataset_name = :d"),
		{"d": _TEST_DATASET},
	).fetchone()
	assert row.import_status == "SUCCESS"
	assert row.attempts == 0
	assert row.failure_category is None
	assert row.last_error is None
	assert row.row_count == 100


def test_claim_lease_prevents_a_second_claim_within_the_window(db):
	claim_time = datetime.now(timezone.utc)
	assert _claim(db, "pinellas_fl", _TEST_DATASET, claim_time) is True
	# Immediately re-claiming within the lease window must fail.
	assert _claim(db, "pinellas_fl", _TEST_DATASET, claim_time + timedelta(minutes=5)) is False


def test_claim_lease_expires_and_becomes_reclaimable(db):
	claim_time = datetime.now(timezone.utc)
	assert _claim(db, "pinellas_fl", _TEST_DATASET, claim_time) is True
	later = claim_time + timedelta(hours=3)  # past the 2-hour lease
	assert _claim(db, "pinellas_fl", _TEST_DATASET, later) is True


def test_claim_never_reclaims_a_failed_permanent_dataset(db):
	db.execute(text("UPDATE assessor_sync_state SET import_status = 'FAILED_PERMANENT' WHERE dataset_name = :d"), {"d": _TEST_DATASET})
	db.commit()
	claim_time = datetime.now(timezone.utc)
	assert _claim(db, "pinellas_fl", _TEST_DATASET, claim_time) is False


if __name__ == "__main__":
	raise SystemExit(pytest.main([__file__, "-v"]))
