"""charge_installment() tests against a FakeSession + FakeStripeGateway —
no live DB, no Stripe. Exercises the ACH/card-fallback/transport-error
decision logic that is the core correctness surface of the settlement
charge path.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services.settlement.charge import charge_installment
from src.services.settlement.gateway import InvoiceHandle, PayOutcome, StripeGateway
from src.services.settlement.store import EvidencePacketStore

_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)

_ROW = SimpleNamespace(
	transaction_id=1, client_id="acme", company_id="co1", opportunity_id="opp-1", door_count=3,
	installment_1_cents=5_000, installment_2_cents=5_000, inst1_attempts=0, inst2_attempts=0,
	evidence_packet_url="https://files.stripe.com/already-published.pdf",
	door_signed_at=_AS_OF, pms_agreement_id=1, agreement_source="PMS_SYNC", agreement_status="ACTIVE",
	stripe_customer_id="cus_123", ach_payment_method_id_encrypted="enc-ach", card_payment_method_id_encrypted="enc-card",
)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row

	def one(self):
		return self._row

	def all(self):
		return [self._row] if self._row else []


class _FakeSession:
	"""Dispatches on a substring of the SQL text — enough to answer
	charge_installment's own queries without a real DB."""

	def __init__(self, row=_ROW):
		self.row = row
		self.executed = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		self.executed.append(sql)
		if "FROM settlement_transactions t" in sql:
			return _FakeResult(self.row)
		return _FakeResult(None)

	def rollback(self):
		pass


class _FakeGateway(StripeGateway):
	def __init__(self, pay_outcomes):
		self._pay_outcomes = list(pay_outcomes)
		self.pay_calls = []
		self.idempotency_keys = []

	def create_invoice(self, *, stripe_customer_id, default_payment_method_id, metadata, idempotency_key):
		self.idempotency_keys.append(idempotency_key)
		return InvoiceHandle(stripe_invoice_id="in_123", status="draft")

	def add_invoice_item(self, **kw):
		self.idempotency_keys.append(kw["idempotency_key"])

	def update_invoice_metadata(self, **kw):
		pass

	def finalize_invoice(self, *, stripe_invoice_id, idempotency_key):
		self.idempotency_keys.append(idempotency_key)
		return InvoiceHandle(stripe_invoice_id=stripe_invoice_id, status="open")

	def pay_invoice(self, *, stripe_invoice_id, payment_method_id, idempotency_key):
		self.pay_calls.append(payment_method_id)
		self.idempotency_keys.append(idempotency_key)
		return self._pay_outcomes.pop(0)

	def void_or_delete_invoice(self, **kw):
		pass


class _StubStore(EvidencePacketStore):
	def publish(self, **kw):
		return "https://files.stripe.com/should-not-be-called.pdf"


@pytest.fixture(autouse=True)
def _patch_decrypt(monkeypatch):
	monkeypatch.setattr("src.services.settlement.charge.decrypt_token", lambda s: s)


def test_ach_paid_charges_with_ach_rail():
	session = _FakeSession()
	gw = _FakeGateway([PayOutcome(status="paid")])
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	assert outcome.status == "CHARGED"
	assert outcome.rail == "ACH"
	assert len(gw.pay_calls) == 1  # no card fallback attempted


def test_definite_ach_decline_falls_back_to_card_and_charges():
	session = _FakeSession()
	gw = _FakeGateway([
		PayOutcome(status="open", error_code="card_declined", error_message="declined"),
		PayOutcome(status="paid"),
	])
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	assert outcome.status == "CHARGED"
	assert outcome.rail == "CARD"
	assert len(gw.pay_calls) == 2


def test_ach_processing_settles_without_card_attempt():
	session = _FakeSession()
	gw = _FakeGateway([PayOutcome(status="processing")])
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	assert outcome.status == "SETTLING"
	assert len(gw.pay_calls) == 1  # never attempts card on an indeterminate ACH result


def test_transport_error_is_uncertain_without_card_attempt():
	session = _FakeSession()
	gw = _FakeGateway([PayOutcome(status="processing", is_transport_error=True, error_message="timeout")])
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	assert outcome.status == "UNCERTAIN"
	assert len(gw.pay_calls) == 1  # the double-charge test: no fallback on an indeterminate result


def test_idempotency_keys_have_no_timestamp_or_attempt_counter():
	session = _FakeSession()
	gw = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	for key in gw.idempotency_keys:
		assert key.startswith("settlement-")
		assert "|1|1" in key  # transaction_id=1, installment=1 only — no volatile suffix


def test_idempotency_key_identical_across_a_deferred_retry():
	"""PR #30 review, 'no duplicate charge across retries': a row deferred
	once (e.g. an evidence-packet-publish failure on attempt 1) and re-claimed
	on attempt 2 must produce byte-identical idempotency keys on the
	eventual successful charge — the key is derived only from
	transaction_id/installment, never inst{N}_attempts, so a retried sweep
	re-derives the same key and Stripe dedupes rather than double-charging."""
	row_first_attempt = SimpleNamespace(**{**vars(_ROW), "inst1_attempts": 0})
	row_second_attempt = SimpleNamespace(**{**vars(_ROW), "inst1_attempts": 1})

	gw1 = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_first_attempt), 1, 1, as_of=_AS_OF, gateway=gw1, store=_StubStore())

	gw2 = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_second_attempt), 1, 1, as_of=_AS_OF, gateway=gw2, store=_StubStore())

	assert gw1.idempotency_keys == gw2.idempotency_keys
