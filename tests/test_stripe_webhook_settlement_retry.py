"""Regression test for PR #37 third review finding #2:
src/api/stripe_webhook_router.py's _handle_settlement_event() used to call
mark_installment_failed with attempts hardcoded to 0, so a webhook-driven
ACH invoice.payment_failed could never cross the 3-attempt
FAILED_PERMANENT threshold. Exercises _handle_settlement_event directly
against a fake session — no live DB, no real Stripe signature.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.api import stripe_webhook_router


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row


class _FakeWebhookSession:
	"""Tracks just enough settlement_transactions state to run the real
	mark_installment/mark_installment_failed SQL through and observe the
	resulting status — dispatches on a substring of the SQL text, same
	convention as tests/test_settlement_charge_gateway.py's _FakeSession."""

	def __init__(self, attempts: int):
		self.attempts = attempts
		self.status = "SETTLING"
		self.alerted_permanent = False

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "INSERT INTO stripe_webhook_events" in sql:
			return _FakeResult(None)  # always a first delivery in this test
		if "SELECT inst1_attempts AS attempts" in sql:
			return _FakeResult(SimpleNamespace(attempts=self.attempts))
		if "UPDATE settlement_transactions SET" in sql and "installment_1_status" in sql:
			self.status = params["status"]
			return _FakeResult(None)
		if "inst1_alerted_permanent_at = NOW()" in sql:
			if self.alerted_permanent:
				return _FakeResult(None)
			self.alerted_permanent = True
			return _FakeResult(SimpleNamespace(transaction_id=1))
		if "UPDATE stripe_webhook_events SET processed_at" in sql:
			return _FakeResult(None)
		return _FakeResult(None)

	def rollback(self):
		pass


def _payment_failed_event(event_id: str) -> dict:
	return {
		"id": event_id,
		"type": "invoice.payment_failed",
		"data": {
			"object": {
				"id": "in_123",
				"metadata": {"client_id": "acme_pm", "transaction_id": "1", "installment": "1"},
				"last_finalization_error": {"message": "ach_debit_failed"},
			}
		},
	}


def _fire(monkeypatch, session, event: dict):
	fake_ctx = MagicMock()
	fake_ctx.__enter__.return_value = session
	fake_ctx.__exit__.return_value = False
	monkeypatch.setattr(stripe_webhook_router, "get_db_context", lambda client_id=None: fake_ctx)
	return stripe_webhook_router._handle_settlement_event(event)


def test_webhook_failure_reads_persisted_attempts_not_hardcoded_zero(monkeypatch):
	"""A single webhook-driven failure at attempt count 1 (below the
	3-attempt threshold) must land on FAILED, not loop forever misreading
	attempts as 0 on every delivery."""
	session = _FakeWebhookSession(attempts=1)
	result = _fire(monkeypatch, session, _payment_failed_event("evt_1"))
	assert result == {"status": "recorded"}
	assert session.status == "FAILED"


def test_three_webhook_driven_ach_failures_reach_failed_permanent(monkeypatch):
	"""Simulates claim_installment_1 incrementing inst1_attempts before each
	of three synchronous charge attempts, each followed by an async
	invoice.payment_failed webhook delivery — the third must cross the
	_MAX_ATTEMPTS_BEFORE_FAILED_PERMANENT=3 threshold and land on
	FAILED_PERMANENT, never retried again."""
	session = _FakeWebhookSession(attempts=1)
	_fire(monkeypatch, session, _payment_failed_event("evt_1"))
	assert session.status == "FAILED"

	session.attempts = 2
	_fire(monkeypatch, session, _payment_failed_event("evt_2"))
	assert session.status == "FAILED"

	session.attempts = 3
	_fire(monkeypatch, session, _payment_failed_event("evt_3"))
	assert session.status == "FAILED_PERMANENT"
