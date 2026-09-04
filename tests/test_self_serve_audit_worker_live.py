"""Live-DB regression for the self-serve worker's retry accounting.

The bug: claim_submissions() increments attempts in the same transaction
that processes the whole batch, and a scoring failure used to call a full
session.rollback() — which also rolled back that increment. A persistently
failing job therefore never reached FAILED_PERMANENT (attempts never grew),
and the rollback discarded successful work for earlier rows in the batch.

These tests need real Postgres because they turn on SAVEPOINT semantics and
on the attempts increment actually persisting across sweeps — a FakeSession
can't reproduce either.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.tasks import self_serve_audit_worker as worker

_DOMAIN = "retry-fail.example.com"


@pytest.fixture
def pending_submission():
	from src.loaders.base import BaseIngestLoader
	company_id = BaseIngestLoader.compute_company_id(_DOMAIN)
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM self_serve_audit_submissions WHERE domain_normalized = :d"), {"d": _DOMAIN})
		s.execute(text("DELETE FROM owner_visibility_scores WHERE company_id = :c"), {"c": company_id})
		s.execute(text("DELETE FROM companies WHERE company_id = :c"), {"c": company_id})
		sid = s.execute(text(
			"INSERT INTO self_serve_audit_submissions "
			"(domain_submitted, domain_normalized, visitor_name, visitor_email, visitor_company, "
			" county_slug, status) "
			"VALUES (:d, :d, 'Test', 't@example.com', 'Retry Co', "
			"(SELECT county_slug FROM counties LIMIT 1), 'PENDING') RETURNING submission_id"
		), {"d": _DOMAIN}).scalar()
	yield {"submission_id": sid, "company_id": company_id}
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM self_serve_audit_submissions WHERE domain_normalized = :d"), {"d": _DOMAIN})
		s.execute(text("DELETE FROM owner_visibility_scores WHERE company_id = :c"), {"c": company_id})
		s.execute(text("DELETE FROM companies WHERE company_id = :c"), {"c": company_id})


def _row(submission_id):
	with get_owner_db_context() as s:
		return s.execute(text(
			"SELECT status, attempts, next_retry_at FROM self_serve_audit_submissions WHERE submission_id = :id"
		), {"id": submission_id}).one()


def _force_due(submission_id):
	with get_owner_db_context() as s:
		s.execute(text("UPDATE self_serve_audit_submissions SET next_retry_at = NULL WHERE submission_id = :id"),
				  {"id": submission_id})


def test_persistent_scoring_failure_reaches_failed_permanent(pending_submission):
	sid = pending_submission["submission_id"]
	with patch.object(worker, "score_one_company", side_effect=RuntimeError("boom")):
		# 1st sweep: attempts 0 -> 1, FAILED with a backoff.
		worker.run_sweep()
		r1 = _row(sid)
		assert r1.status == "FAILED"
		assert r1.attempts == 1, "the claimed attempts increment must survive the scoring rollback"

		# 2nd sweep (forced due): 1 -> 2, still FAILED.
		_force_due(sid)
		worker.run_sweep()
		r2 = _row(sid)
		assert r2.status == "FAILED"
		assert r2.attempts == 2

		# 3rd sweep (forced due): 2 -> 3, now terminal.
		_force_due(sid)
		worker.run_sweep()
		r3 = _row(sid)
		assert r3.attempts == 3
		assert r3.status == "FAILED_PERMANENT"
		assert r3.next_retry_at is None


def test_a_failing_row_does_not_discard_an_earlier_scored_row(pending_submission):
	"""A second, good submission scored in the same batch must not be rolled
	back when a later submission's scoring fails."""
	good_domain = "retry-good.example.com"
	from src.loaders.base import BaseIngestLoader
	good_company = BaseIngestLoader.compute_company_id(good_domain)
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM self_serve_audit_submissions WHERE domain_normalized = :d"), {"d": good_domain})
		s.execute(text("DELETE FROM owner_visibility_scores WHERE company_id = :c"), {"c": good_company})
		s.execute(text("DELETE FROM companies WHERE company_id = :c"), {"c": good_company})
		good_id = s.execute(text(
			"INSERT INTO self_serve_audit_submissions "
			"(domain_submitted, domain_normalized, visitor_name, visitor_email, visitor_company, county_slug, status) "
			"VALUES (:d, :d, 'Good', 'g@example.com', 'Good Co', (SELECT county_slug FROM counties LIMIT 1), 'PENDING') "
			"RETURNING submission_id"
		), {"d": good_domain}).scalar()
	bad_id = pending_submission["submission_id"]

	def _score(session, company, month_key):
		if company["domain"] == good_domain:
			return  # succeed
		raise RuntimeError("boom")  # the other submission fails

	try:
		with patch.object(worker, "score_one_company", side_effect=_score):
			worker.run_sweep()
		good = _row(good_id)
		bad = _row(bad_id)
		assert good.status == "SCORED", "an earlier scored row must survive a later row's failure"
		assert bad.status == "FAILED"
	finally:
		with get_owner_db_context() as s:
			s.execute(text("DELETE FROM self_serve_audit_submissions WHERE domain_normalized = :d"), {"d": good_domain})
			s.execute(text("DELETE FROM owner_visibility_scores WHERE company_id = :c"), {"c": good_company})
			s.execute(text("DELETE FROM companies WHERE company_id = :c"), {"c": good_company})
