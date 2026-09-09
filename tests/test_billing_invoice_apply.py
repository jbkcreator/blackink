"""Pure tests: apply_pending_credits_to_invoice() only applies credits whose
own billing_period matches the invoice being built (PR #37 review finding —
'filter pending credits by the intended billing period before applying
them'), and stores the returned invoice ITEM id, not the parent invoice id
(PR #37 review finding — 'store the actual Stripe invoice-item ID')."""
from contextlib import contextmanager
from datetime import date, datetime, timezone

from src.services.billing.invoice_apply import apply_pending_credits_to_invoice


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)


class _FakeGateway:
	def __init__(self):
		self.add_invoice_item_calls = []

	def add_invoice_item(self, **kw):
		self.add_invoice_item_calls.append(kw)
		return "ii_fake_item_id"


class _FakeSession:
	def __init__(self, credit_rows_by_period: dict[date, list[_Row]]):
		self._credit_rows_by_period = credit_rows_by_period
		self.updates = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "SELECT credit_id" in sql:
			rows = self._credit_rows_by_period.get(params["billing_period"], [])
			return type("R", (), {"all": lambda self=None: rows})()
		if "UPDATE billing_credits" in sql:
			self.updates.append(params)
			return type("R", (), {})()
		return type("R", (), {"all": lambda self=None: []})()

	def begin_nested(self):
		@contextmanager
		def _cm():
			yield

		return _cm()


def test_only_credits_for_the_matching_billing_period_are_applied():
	september = date(2026, 9, 1)
	october = date(2026, 10, 1)
	session = _FakeSession({
		september: [_Row(credit_id=1, credit_type="MISS_CREDIT", amount_cents=5000)],
		october: [_Row(credit_id=2, credit_type="MISS_CREDIT", amount_cents=5000)],
	})
	gw = _FakeGateway()

	applied = apply_pending_credits_to_invoice(
		session, client_id="acme_pm", billing_period=september,
		stripe_invoice_id="in_123", stripe_customer_id="cus_1",
		as_of=datetime(2026, 9, 15, tzinfo=timezone.utc), gateway=gw,
	)
	assert applied == 1
	assert len(gw.add_invoice_item_calls) == 1
	assert session.updates[0]["credit_id"] == 1


def test_stores_the_invoice_item_id_not_the_parent_invoice_id():
	september = date(2026, 9, 1)
	session = _FakeSession({
		september: [_Row(credit_id=1, credit_type="MISS_CREDIT", amount_cents=5000)],
	})
	gw = _FakeGateway()

	apply_pending_credits_to_invoice(
		session, client_id="acme_pm", billing_period=september,
		stripe_invoice_id="in_parent_invoice", stripe_customer_id="cus_1",
		as_of=datetime(2026, 9, 15, tzinfo=timezone.utc), gateway=gw,
	)
	assert session.updates[0]["invoice_item_id"] == "ii_fake_item_id"
	assert session.updates[0]["invoice_item_id"] != "in_parent_invoice"
