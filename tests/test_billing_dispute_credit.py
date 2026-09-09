"""Pure tests for the 48h dispute window boundary (Subtask 1.2.3, rule 4).
No DB — a minimal fake Session standing in for the two SELECTs
credit_dispute_on_flag() issues."""
from datetime import datetime, timedelta, timezone

import pytest

from src.services.billing.dispute_credit import (
	DISPUTE_WINDOW_HOURS,
	DisputeWindowExpiredError,
	MissingBilledAmountError,
	credit_dispute_on_flag,
)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row

	def one(self):
		return self._row


class _FakeSession:
	def __init__(self, dispute_row, offer_row):
		self._dispute_row = dispute_row
		self._offer_row = offer_row
		self.executed = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		self.executed.append((sql, params))
		if "appointment_disputes" in sql:
			return _FakeResult(self._dispute_row)
		if "entitlement_offers" in sql:
			return _FakeResult(self._offer_row)
		if "billing_credits" in sql:
			# credit insert path — return a fake row with credit_id
			return _FakeResult(type("R", (), {"credit_id": 1})())
		return _FakeResult(None)

	def begin_nested(self):
		from contextlib import contextmanager

		@contextmanager
		def _cm():
			yield

		return _cm()


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)


def _make_session(scheduled_for: datetime, flagged_at: datetime, client_id="acme_pm", appointment_id="appt-1", billed_amount_cents=9900):
	dispute_row = _Row(
		client_id=client_id, appointment_id=appointment_id, flagged_at=flagged_at,
		scheduled_for=scheduled_for, billed_amount_cents=billed_amount_cents,
	)
	offer_row = _Row(price_cents=9900)
	return _FakeSession(dispute_row, offer_row)


def test_dispute_within_48h_window_is_accepted():
	scheduled_for = datetime(2026, 1, 1, tzinfo=timezone.utc)
	flagged_at = scheduled_for + timedelta(hours=47)
	session = _make_session(scheduled_for, flagged_at)
	# credit_dispute_on_flag calls issue_credit which itself calls session.execute
	# for the INSERT — patch issue_credit to avoid needing a full fake of it.
	import src.services.billing.dispute_credit as mod

	def fake_issue_credit(session, **kwargs):
		return True

	orig = mod.issue_credit
	mod.issue_credit = fake_issue_credit
	try:
		result = credit_dispute_on_flag(session, dispute_id="dispute-1", as_of=flagged_at)
	finally:
		mod.issue_credit = orig
	assert result is True


def test_dispute_outside_48h_window_is_rejected():
	scheduled_for = datetime(2026, 1, 1, tzinfo=timezone.utc)
	flagged_at = scheduled_for + timedelta(hours=49)
	session = _make_session(scheduled_for, flagged_at)
	with pytest.raises(DisputeWindowExpiredError):
		credit_dispute_on_flag(session, dispute_id="dispute-1", as_of=flagged_at)


def test_window_constant_is_48_hours():
	assert DISPUTE_WINDOW_HOURS == 48


def test_dispute_with_no_billed_amount_blocks_instead_of_guessing():
	"""PR #37 review finding — a disputed appointment with no
	billed_amount_cents recorded must raise and mark the row BLOCKED, never
	fall back to appt_standard's current price (that fallback previously
	overcredited a disputed FREE first sit as a $99 standard sit)."""
	scheduled_for = datetime(2026, 1, 1, tzinfo=timezone.utc)
	flagged_at = scheduled_for + timedelta(hours=1)
	session = _make_session(scheduled_for, flagged_at, billed_amount_cents=None)
	with pytest.raises(MissingBilledAmountError):
		credit_dispute_on_flag(session, dispute_id="dispute-1", as_of=flagged_at)
	blocked_updates = [
		params for sql, params in session.executed
		if params and params.get("dispute_id") == "dispute-1" and "credit_status = 'BLOCKED'" in sql
	]
	assert len(blocked_updates) == 1
