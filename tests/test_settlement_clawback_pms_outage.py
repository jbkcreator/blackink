"""Regression tests for PR #30 review finding 2: a day-60 PMS "cannot
determine" used to mark installment 2 terminally BLOCKED, and
claim_installment_2() never re-selected BLOCKED rows — so a transient PMS
outage permanently forfeited the second 50% of every bounty (the wired
StubPmsProvider always returns None, so this was every currently deployed
row's fate).

No live DB: a FakeSession stands in, matching
tests/test_settlement_requires_published_packet.py's style.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.services.settlement.clawback import process_installment_2

_SIGNED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_WINDOW = 60


def _row(*, terminated_at=None, inst2_attempts=0):
	return SimpleNamespace(
		transaction_id=1, client_id="acme", installment_2_cents=5_000, inst2_stripe_invoice_id=None,
		inst2_attempts=inst2_attempts,
		pms_agreement_id=1, pms_property_ref="ref-1", door_signed_at=_SIGNED, terminated_at=terminated_at,
		clawback_window_days=_WINDOW,
	)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def one(self):
		return self._row

	def first(self):
		return self._row


class _FakeSession:
	def __init__(self, row):
		self.row = row
		self.updates = []
		self.events = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "FROM settlement_transactions t" in sql:
			return _FakeResult(self.row)
		if "installment_2_status" in sql:
			self.updates.append(params)
		return _FakeResult(None)


class _NeverDeterminePms:
	def is_agreement_active(self, ref):
		return None


class _NowActivePms:
	def is_agreement_active(self, ref):
		return True


class _NowTerminatedInWindowPms:
	def is_agreement_active(self, ref):
		return False


def test_pms_unavailable_defers_with_reason_and_retry_time_not_terminal():
	session = _FakeSession(_row())
	as_of = _SIGNED + timedelta(days=61)

	status = process_installment_2(session, 1, as_of=as_of, pms=_NeverDeterminePms())

	assert status == "BLOCKED"
	assert len(session.updates) == 1
	params = session.updates[0]
	assert params["status"] == "BLOCKED"
	assert params["blocked_reason"] == "PMS_VERIFICATION_UNAVAILABLE"
	assert params["next_retry_at"] is not None
	assert params["next_retry_at"] > as_of  # reclaimable LATER, never immediately re-selected


def test_five_consecutive_pms_failures_emit_exactly_one_alert(monkeypatch):
	"""PR #30 review: 'five failures generate one alert without stopping
	retries' — assert the event fires exactly once, on the 5th attempt, not
	on every tick before or after."""
	logged = []
	monkeypatch.setattr(
		"src.services.settlement.clawback.log_event",
		lambda *a, **k: logged.append(k.get("payload")),
	)
	as_of = _SIGNED + timedelta(days=61)

	for attempts_before_this_call in range(3, 9):  # ticks 4..9 attempts recorded
		session = _FakeSession(_row(inst2_attempts=attempts_before_this_call))
		process_installment_2(session, 1, as_of=as_of, pms=_NeverDeterminePms())

	assert len(logged) == 1
	assert logged[0]["attempts"] == 5
	assert logged[0]["error_code"] == "PMS_VERIFICATION_UNAVAILABLE"


def test_pms_recovery_to_active_charges(monkeypatch):
	"""A row previously deferred for PMS unavailability must actually charge
	once the provider resolves to active — not stay parked forever."""
	charged = {}

	def _fake_charge(session, transaction_id, installment, *, as_of, gateway=None, store=None):
		charged["called"] = (transaction_id, installment)
		return SimpleNamespace(status="CHARGED")

	monkeypatch.setattr("src.services.settlement.clawback.charge_installment", _fake_charge)
	session = _FakeSession(_row(inst2_attempts=2))
	as_of = _SIGNED + timedelta(days=61)

	status = process_installment_2(session, 1, as_of=as_of, pms=_NowActivePms())

	assert status == "CHARGED"
	assert charged["called"] == (1, 2)


def test_pms_recovery_to_terminated_in_window_voids_and_claws_back():
	session = _FakeSession(_row(terminated_at=_SIGNED + timedelta(days=30), inst2_attempts=2))
	as_of = _SIGNED + timedelta(days=61)

	status = process_installment_2(session, 1, as_of=as_of, pms=_NowTerminatedInWindowPms())

	assert status == "VOIDED_CLAWBACK"
	assert len(session.updates) == 1
	assert session.updates[0]["status"] == "VOIDED_CLAWBACK"
