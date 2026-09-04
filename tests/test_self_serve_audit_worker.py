"""Unit tests for src/tasks/self_serve_audit_worker.py's retry/terminal-
state logic and county-context gating (Subtask 3.2.3 corrections) — no
live DB, using a small scripted FakeSession that returns canned results
in call order, and score_one_company mocked out so these tests never
touch real signal providers.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.tasks.self_serve_audit_worker import (
	_MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT,
	_mark_failed,
	process_submission,
)


class _Result:
	def __init__(self, row=None):
		self._row = row

	def one(self):
		return self._row

	def first(self):
		return self._row


class FakeSession:
	"""Returns queued results in call order, records every UPDATE's params
	so tests can assert the final status/next_retry_at without a real DB."""

	def __init__(self, results):
		self._results = list(results)
		self.updates = []
		self.rolled_back = False

	def rollback(self):
		self.rolled_back = True

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if sql.strip().upper().startswith("UPDATE SELF_SERVE_AUDIT_SUBMISSIONS"):
			self.updates.append(params)
			return _Result()
		if sql.strip().upper().startswith("INSERT INTO COMPANIES"):
			return _Result()
		return self._results.pop(0)


_SUBMISSION_ROW = SimpleNamespace(
	submission_id=1, domain_normalized="acme-pm.example.com", visitor_company="Acme PM",
	county_slug="hillsborough_fl", attempts=1,
)


def test_blocked_missing_county_when_submission_has_no_county():
	"""companies.county_slug is NOT NULL -- checked on the submission row
	itself, before any companies INSERT is even attempted (that INSERT
	would otherwise crash on the NOT NULL constraint)."""
	no_county_row = SimpleNamespace(**{**_SUBMISSION_ROW.__dict__, "county_slug": None})
	session = FakeSession([_Result(no_county_row)])
	process_submission(session, 1)
	assert session.updates[-1]["status"] == "BLOCKED_MISSING_COUNTY"
	assert session.updates[-1]["company_id"] is None


def test_skipped_recent_when_already_scored_this_month():
	session = FakeSession([
		_Result(_SUBMISSION_ROW),
		_Result(SimpleNamespace()),  # existing owner_visibility_scores row -> truthy
	])
	process_submission(session, 1)
	assert session.updates[-1]["status"] == "SKIPPED_RECENT"


def test_scored_on_success_with_real_county():
	session = FakeSession([
		_Result(_SUBMISSION_ROW),
		_Result(None),
		_Result(SimpleNamespace(
			company_id="cid1", company_name="Acme PM", domain="acme-pm.example.com",
			website=None, county_slug="hillsborough_fl", google_place_id=None,
		)),
	])
	with patch("src.tasks.self_serve_audit_worker.score_one_company") as mock_score:
		process_submission(session, 1)
	mock_score.assert_called_once()
	# The company_name passed downstream is the visitor-submitted name,
	# never the raw domain.
	called_company = mock_score.call_args.args[1]
	assert called_company["company_name"] == "Acme PM"
	assert session.updates[-1]["status"] == "SCORED"


def test_scoring_failure_retries_with_backoff_before_bound():
	session = FakeSession([
		_Result(SimpleNamespace(**{**_SUBMISSION_ROW.__dict__, "attempts": 1})),
		_Result(None),
		_Result(SimpleNamespace(
			company_id="cid1", company_name="Acme PM", domain="acme-pm.example.com",
			website=None, county_slug="hillsborough_fl", google_place_id=None,
		)),
	])
	with patch("src.tasks.self_serve_audit_worker.score_one_company", side_effect=RuntimeError("boom")):
		process_submission(session, 1)
	assert session.updates[-1]["status"] == "FAILED"
	assert session.updates[-1]["next_retry_at"] is not None


def test_scoring_failure_becomes_permanent_at_bound():
	session = FakeSession([
		_Result(SimpleNamespace(**{**_SUBMISSION_ROW.__dict__, "attempts": _MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT})),
		_Result(None),
		_Result(SimpleNamespace(
			company_id="cid1", company_name="Acme PM", domain="acme-pm.example.com",
			website=None, county_slug="hillsborough_fl", google_place_id=None,
		)),
	])
	with patch("src.tasks.self_serve_audit_worker.score_one_company", side_effect=RuntimeError("boom")):
		process_submission(session, 1)
	assert session.updates[-1]["status"] == "FAILED_PERMANENT"
	assert session.updates[-1]["next_retry_at"] is None


def test_invalid_domain_is_permanent_not_retried():
	bad_row = SimpleNamespace(
		submission_id=1, domain_normalized="", visitor_company="Acme PM",
		county_slug="hillsborough_fl", attempts=0,
	)
	session = FakeSession([_Result(bad_row)])
	process_submission(session, 1)
	assert session.updates[-1]["status"] == "FAILED_PERMANENT"


def test_mark_failed_uses_exponential_backoff():
	session = FakeSession([])
	_mark_failed(session, 1, attempts=2, error="boom")
	assert session.updates[-1]["status"] == "FAILED"
	assert session.updates[-1]["next_retry_at"] is not None
