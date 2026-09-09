"""Fail-closed rule: a SYNTHETIC agreement may only charge under a Stripe
test-mode key (sk_test_); in live mode it must refuse with zero gateway
calls. A PMS_SYNC agreement proceeds under both."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.services.settlement.charge import charge_installment
from src.services.settlement.gateway import PayOutcome, StripeGateway, InvoiceHandle
from src.services.settlement.store import EvidencePacketStore

_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(agreement_source: str) -> SimpleNamespace:
	return SimpleNamespace(
		transaction_id=1, client_id="acme", company_id="co1", opportunity_id="opp-1", door_count=3,
		installment_1_cents=5_000, installment_2_cents=5_000, inst1_attempts=0, inst2_attempts=0,
		inst1_reopen_count=0, inst2_reopen_count=0,
		inst1_stripe_invoice_id=None, inst2_stripe_invoice_id=None,
		evidence_packet_url="https://files.stripe.com/already-published.pdf",
		door_signed_at=_AS_OF, pms_agreement_id=1, agreement_source=agreement_source, agreement_status="ACTIVE",
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
	def __init__(self, row):
		self.row = row

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "FROM settlement_transactions t" in sql:
			return _FakeResult(self.row)
		if "UPDATE settlement_transactions SET" in sql and "stripe_invoice_id" in sql and params:
			self.row.inst1_stripe_invoice_id = params.get("invoice_id", self.row.inst1_stripe_invoice_id)
		return _FakeResult(None)

	def begin_nested(self):
		from contextlib import contextmanager

		@contextmanager
		def _cm():
			yield

		return _cm()

	def rollback(self):
		pass


class _RecordingGateway(StripeGateway):
	def __init__(self, pay_status="paid"):
		self.calls = []
		self._pay_status = pay_status

	def create_invoice(self, **kw):
		self.calls.append("create_invoice")
		return InvoiceHandle(stripe_invoice_id="in_123", status="draft")

	def find_invoice_by_metadata(self, **kw):
		self.calls.append("find_invoice_by_metadata")
		return None

	def add_invoice_item(self, **kw):
		self.calls.append("add_invoice_item")

	def update_invoice_metadata(self, **kw):
		self.calls.append("update_invoice_metadata")

	def finalize_invoice(self, **kw):
		self.calls.append("finalize_invoice")
		return InvoiceHandle(stripe_invoice_id="in_123", status="open")

	def pay_invoice(self, **kw):
		self.calls.append("pay_invoice")
		return PayOutcome(status=self._pay_status)

	def void_or_delete_invoice(self, **kw):
		self.calls.append("void_or_delete_invoice")


class _StubStore(EvidencePacketStore):
	def publish(self, **kw):
		return "https://files.stripe.com/x.pdf"


@pytest.fixture(autouse=True)
def _patch_decrypt(monkeypatch):
	monkeypatch.setattr("src.services.settlement.charge.decrypt_token", lambda s: s)


def _patch_test_mode(monkeypatch, is_test_mode: bool):
	monkeypatch.setattr("src.services.settlement.charge._is_stripe_test_mode", lambda: is_test_mode)


def test_synthetic_agreement_refused_in_live_mode(monkeypatch):
	_patch_test_mode(monkeypatch, is_test_mode=False)
	session = _FakeSession(_row("SYNTHETIC"))
	gw = _RecordingGateway()
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	assert outcome.status == "BLOCKED"
	assert outcome.reason == "SYNTHETIC_AGREEMENT_IN_LIVE_MODE"
	assert gw.calls == []


def test_synthetic_agreement_proceeds_in_test_mode(monkeypatch):
	_patch_test_mode(monkeypatch, is_test_mode=True)
	session = _FakeSession(_row("SYNTHETIC"))
	gw = _RecordingGateway(pay_status="paid")
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	assert outcome.status == "CHARGED"
	assert "create_invoice" in gw.calls


def test_pms_sync_agreement_proceeds_regardless_of_mode(monkeypatch):
	for test_mode in (True, False):
		_patch_test_mode(monkeypatch, is_test_mode=test_mode)
		session = _FakeSession(_row("PMS_SYNC"))
		gw = _RecordingGateway(pay_status="paid")
		outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
		assert outcome.status == "CHARGED"
