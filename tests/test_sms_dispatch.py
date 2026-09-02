"""Pure unit tests (no live DB) for Week 1 Subtask 1.2.3's application-layer
cold-SMS linter. Mirrors test_campaign_readiness_gate.py's FakeSession
pattern. The DB-layer CHECK constraint and the full dispatch_sms() path
(including sms_dispatch_log/events writes) are covered separately in
tests/test_tenant_isolation.py, which requires a live Postgres.
"""

from types import SimpleNamespace

import pytest

from src.services.sms_dispatch import ColdSMSBlockedError, SmsProvider, dispatch_sms


class _CountingSmsProvider(SmsProvider):
	def __init__(self):
		self.calls = []

	def send(self, phone, message):
		self.calls.append((phone, message))
		return "stub-message-id"


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def one(self):
		return self._row


class _FakeSession:
	def __init__(self, contact_row):
		self._contact_row = contact_row
		self.executed = []

	def execute(self, stmt, params=None):
		self.executed.append((str(stmt), params))
		if "SELECT phone" in str(stmt):
			return _FakeResult(self._contact_row)
		return _FakeResult(None)


def _cold_contact(phone="+15551234567"):
	return SimpleNamespace(phone=phone, inbound_sms_count=0, booked_appointment_id=None)


def _engaged_contact(phone="+15551234567", inbound_sms_count=1, booked_appointment_id=None):
	return SimpleNamespace(
		phone=phone, inbound_sms_count=inbound_sms_count, booked_appointment_id=booked_appointment_id
	)


def test_cold_contact_raises_and_never_calls_provider():
	session = _FakeSession(_cold_contact())
	provider = _CountingSmsProvider()

	with pytest.raises(ColdSMSBlockedError):
		dispatch_sms(session, contact_id=1, client_id="client_a", message="hi", sms_provider=provider)

	assert provider.calls == []
	# The linter's own event-logging INSERT should have run before raising.
	assert any("cold_sms_blocked" in stmt for stmt, _ in session.executed)


def test_cold_contact_blocked_event_carries_application_layer():
	session = _FakeSession(_cold_contact())
	provider = _CountingSmsProvider()

	with pytest.raises(ColdSMSBlockedError):
		dispatch_sms(session, contact_id=1, client_id="client_a", message="hi", sms_provider=provider)

	insert_call = next(params for stmt, params in session.executed if "cold_sms_blocked" in stmt)
	assert insert_call["layer"] == "APPLICATION"


def test_engaged_via_inbound_sms_count_calls_provider_once():
	session = _FakeSession(_engaged_contact(inbound_sms_count=3))
	provider = _CountingSmsProvider()

	message_id = dispatch_sms(
		session, contact_id=2, client_id="client_a", message="reminder", sms_provider=provider
	)

	assert message_id == "stub-message-id"
	assert provider.calls == [("+15551234567", "reminder")]


def test_engaged_via_booked_appointment_calls_provider_once():
	session = _FakeSession(_engaged_contact(inbound_sms_count=0, booked_appointment_id="appt_1"))
	provider = _CountingSmsProvider()

	dispatch_sms(session, contact_id=3, client_id="client_a", message="reminder", sms_provider=provider)

	assert len(provider.calls) == 1


def test_engaged_contact_writes_sms_dispatch_log():
	session = _FakeSession(_engaged_contact(inbound_sms_count=1))
	provider = _CountingSmsProvider()

	dispatch_sms(session, contact_id=4, client_id="client_a", message="reminder", sms_provider=provider)

	assert any("INSERT INTO sms_dispatch_log" in stmt for stmt, _ in session.executed)
