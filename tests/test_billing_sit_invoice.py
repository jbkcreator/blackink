"""Pure tests for charge_sit_for_appointment() (PR #37 review — blocking
findings 1/2: resolve_sit_charge()/apply_pending_credits_to_invoice() were
never wired into any production path). No DB — a fake Session/gateway."""
from datetime import datetime, timezone

from src.services.billing.sit_invoice import charge_sit_for_appointment


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)


class _FakeGateway:
	def __init__(self):
		self.calls = []

	def create_invoice(self, **kw):
		self.calls.append(("create_invoice", kw))
		return type("H", (), {"stripe_invoice_id": "in_fake", "status": "draft"})()

	def add_invoice_item(self, **kw):
		self.calls.append(("add_invoice_item", kw))
		return "ii_fake"

	def finalize_invoice(self, **kw):
		self.calls.append(("finalize_invoice", kw))
		return type("H", (), {"stripe_invoice_id": "in_fake", "status": "open"})()


class _FakeSession:
	def __init__(self, appointment_row):
		self._appointment_row = appointment_row
		self.updates = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "JOIN clients c" in sql:
			row = self._appointment_row
			return type("R", (), {"first": lambda _self=None, _row=row: _row})()
		if "UPDATE appointments SET billing_blocked_reason" in sql:
			self.updates.append(params)
			return None
		return type("R", (), {"first": lambda self=None: None, "all": lambda self=None: []})()

	def begin_nested(self):
		from contextlib import contextmanager

		@contextmanager
		def _cm():
			yield

		return _cm()


def test_blocked_when_client_has_no_stripe_customer_id():
	row = _Row(is_billable=True, billed_offer_code=None, billed_amount_cents=None, stripe_customer_id=None)
	session = _FakeSession(row)
	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=_FakeGateway(),
	)
	assert outcome.status == "BLOCKED"
	assert outcome.reason == "NO_STRIPE_CUSTOMER"
	assert session.updates[0]["client_id"] == "acme_pm"


def test_already_invoiced_appointment_makes_no_new_stripe_calls():
	"""PR #37 review finding — retrying the same appointment must never
	create a second Stripe invoice, even though resolve_sit_charge() itself
	is idempotent and would return the same charge again."""
	row = _Row(is_billable=True, billed_offer_code="appt_first", billed_amount_cents=0, stripe_customer_id="cus_1")
	session = _FakeSession(row)
	gw = _FakeGateway()
	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
	)
	assert outcome.status == "ALREADY_INVOICED"
	assert gw.calls == []
