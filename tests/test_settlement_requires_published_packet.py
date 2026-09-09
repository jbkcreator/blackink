"""Fail-closed rule: charge_installment() must refuse to create ANY Stripe
object when the evidence packet did not publish — zero gateway calls."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.services.settlement.charge import charge_installment
from src.services.settlement.gateway import StripeGateway
from src.services.settlement.store import EvidencePacketStore

_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)

_ROW = SimpleNamespace(
	transaction_id=1, client_id="acme", company_id="co1", opportunity_id="opp-1", door_count=3,
	installment_1_cents=5_000, installment_2_cents=5_000, inst1_attempts=0, inst2_attempts=0,
	inst1_reopen_count=0, inst2_reopen_count=0,
	evidence_packet_url=None,  # not yet published
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
	def __init__(self, row):
		self.row = row
		self.updates = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "FROM settlement_transactions t" in sql:
			return _FakeResult(self.row)
		if "evidence_packet_sha256" in sql:
			return _FakeResult(None)  # _publish_evidence_packet's own status UPDATE
		if "installment_1_status" in sql or "installment_2_status" in sql:
			self.updates.append(params)  # mark_installment / defer_installment's UPDATE
		return _FakeResult(None)

	def rollback(self):
		pass


class _RecordingGateway(StripeGateway):
	def __init__(self):
		self.calls = []

	def create_invoice(self, **kw):
		self.calls.append("create_invoice")
		raise AssertionError("must not be called when the evidence packet is unpublished")

	def find_invoice_by_metadata(self, **kw):
		self.calls.append("find_invoice_by_metadata")
		return None

	def retrieve_invoice(self, **kw):
		self.calls.append("retrieve_invoice")
		raise AssertionError("must not be called when the evidence packet is unpublished")

	def add_invoice_item(self, **kw):
		self.calls.append("add_invoice_item")

	def update_invoice_metadata(self, **kw):
		self.calls.append("update_invoice_metadata")

	def finalize_invoice(self, **kw):
		self.calls.append("finalize_invoice")

	def pay_invoice(self, **kw):
		self.calls.append("pay_invoice")

	def void_or_delete_invoice(self, **kw):
		self.calls.append("void_or_delete_invoice")


class _NullStore(EvidencePacketStore):
	"""Simulates StubEvidencePacketStore — no object storage configured."""

	def publish(self, **kw):
		return None


@pytest.fixture(autouse=True)
def _patch_decrypt(monkeypatch):
	monkeypatch.setattr("src.services.settlement.charge.decrypt_token", lambda s: s)


@pytest.fixture(autouse=True)
def _patch_pdf_compile(monkeypatch):
	# Evidence assembly/PDF rendering need a real DB; stub them out so this
	# test exercises only the publish-gate logic in charge.py.
	monkeypatch.setattr(
		"src.services.settlement.charge.assemble_evidence_packet",
		lambda session, *, transaction_id, installment: SimpleNamespace(sections_with_gaps=[]),
	)
	monkeypatch.setattr("src.services.settlement.charge.compile_packet", lambda data: b"%PDF-fake")


def test_unpublished_packet_blocks_with_zero_gateway_calls():
	session = _FakeSession(_ROW)
	gw = _RecordingGateway()
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_NullStore())
	assert outcome.status == "BLOCKED"
	assert outcome.reason == "EVIDENCE_PACKET_UNPUBLISHED"
	assert gw.calls == []


def test_unpublished_packet_is_deferred_not_terminal(monkeypatch):
	"""PR #30 review finding 1: BLOCKED/EVIDENCE_PACKET_UNPUBLISHED must carry
	a reason code and a next_retry_at so claim_installment_1/2 can re-select
	it later — the old code left the row terminally BLOCKED forever."""
	session = _FakeSession(_ROW)
	outcome = charge_installment(session, 1, 1, as_of=_AS_OF, gateway=_RecordingGateway(), store=_NullStore())
	assert outcome.status == "BLOCKED"
	assert len(session.updates) == 1
	params = session.updates[0]
	assert params["status"] == "BLOCKED"
	assert params["blocked_reason"] == "EVIDENCE_PACKET_UNPUBLISHED"
	assert params["next_retry_at"] is not None
