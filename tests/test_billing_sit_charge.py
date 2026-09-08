"""Pure test for rule 2 (first sit free) — resolve_sit_charge() returns
appt_first/$0 for a fresh entitlement, then appt_standard/$99 once
first_sit_consumed is TRUE. No DB — a fake Session."""
from contextlib import contextmanager

from src.services.billing.sit_billing import resolve_sit_charge


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row

	def one(self):
		return self._row


class _FakeSession:
	def __init__(self, first_sit_consumed: bool):
		self.first_sit_consumed = first_sit_consumed
		self.flip_rowcount = 1

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "FROM appointments" in sql:
			return _FakeResult(_Row(is_billable=True))
		if "FROM client_entitlements" in sql:
			return _FakeResult(_Row(entitlement_id=1, first_sit_consumed=self.first_sit_consumed))
		if "FROM entitlement_offers" in sql:
			return _FakeResult(_Row(price_cents=9900))
		if "UPDATE client_entitlements" in sql:
			result = type("R", (), {"rowcount": self.flip_rowcount})()
			return result
		return _FakeResult(None)

	def begin_nested(self):
		@contextmanager
		def _cm():
			yield

		return _cm()


def test_first_sit_charges_zero_and_flips_flag(monkeypatch):
	import src.services.billing.sit_billing as mod

	session = _FakeSession(first_sit_consumed=False)
	# resolve_sit_charge imports log_event locally — patch the module it's imported from.
	monkeypatch.setattr("src.services.events.log_event", lambda *a, **kw: None)
	charge = resolve_sit_charge(session, client_id="acme_pm", appointment_id="appt-1", as_of=None)
	assert charge.offer_code == "appt_first"
	assert charge.amount_cents == 0
	assert charge.first_sit is True


def test_second_sit_charges_standard_price():
	session = _FakeSession(first_sit_consumed=True)
	charge = resolve_sit_charge(session, client_id="acme_pm", appointment_id="appt-2", as_of=None)
	assert charge.offer_code == "appt_standard"
	assert charge.amount_cents == 9900
	assert charge.first_sit is False
