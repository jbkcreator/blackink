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
from src.services.settlement.gateway import InvoiceHandle, LiveStripeGateway, PayOutcome, StripeGateway
from src.services.settlement.store import EvidencePacketStore

_AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)

_ROW = SimpleNamespace(
	transaction_id=1, client_id="acme", company_id="co1", opportunity_id="opp-1", door_count=3,
	installment_1_cents=5_000, installment_2_cents=5_000, inst1_attempts=0, inst2_attempts=0,
	inst1_reopen_count=0, inst2_reopen_count=0,
	inst1_stripe_invoice_id=None, inst2_stripe_invoice_id=None,
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

	def __init__(self, row=None):
		# Copy, never share, the module-level _ROW — this test's new
		# stripe_invoice_id persistence UPDATE mutates self.row, and the
		# default arg is evaluated once at import time, so every test that
		# didn't pass its own row would otherwise silently mutate every
		# other test's starting state.
		self.row = SimpleNamespace(**vars(row if row is not None else _ROW))
		self.executed = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		self.executed.append(sql)
		if "FROM settlement_transactions t" in sql:
			return _FakeResult(self.row)
		if "UPDATE settlement_transactions SET" in sql and "stripe_invoice_id" in sql:
			# The finding-#8-style immediate persist of the invoice id right
			# after finalize_invoice — record it on the fake row so a
			# same-test retry could observe it, same as the real UPDATE would.
			if params and "invoice_id" in params:
				setattr(self.row, "inst1_stripe_invoice_id", params["invoice_id"])
			return _FakeResult(None)
		return _FakeResult(None)

	def begin_nested(self):
		from contextlib import contextmanager

		@contextmanager
		def _cm():
			yield

		return _cm()

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

	def find_invoice_by_metadata(self, **kw):
		return None

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


def test_object_creation_keys_have_no_timestamp_or_attempt_counter():
	"""PR #37 review finding #2: the OBJECT-creation keys (invoice/item/
	finalize) must stay pinned to (transaction_id, installment) alone — this
	is what guarantees exactly one Stripe invoice per installment no matter
	how many times charge_installment() is entered. Only the PAYMENT-attempt
	keys (checked separately below) are allowed to vary by attempt."""
	session = _FakeSession()
	gw = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(session, 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())
	object_keys = [k for k in gw.idempotency_keys if not k.startswith(("settlement-pay-ach|", "settlement-pay-card|"))]
	assert object_keys, "expected at least one object-creation key to check"
	for key in object_keys:
		assert key.startswith("settlement-")
		assert key.endswith("|1|1")  # transaction_id=1, installment=1 only — no volatile suffix


def test_object_creation_key_identical_across_a_deferred_retry():
	"""PR #30 review, 'no duplicate charge across retries': a row deferred
	once (e.g. an evidence-packet-publish failure on attempt 1) and re-claimed
	on attempt 2 must produce a byte-identical INVOICE-creation idempotency
	key on the eventual successful charge — that key is derived only from
	transaction_id/installment, never inst{N}_attempts, so a retried sweep
	re-derives the same key and Stripe dedupes the invoice rather than
	double-creating it."""
	row_first_attempt = SimpleNamespace(**{**vars(_ROW), "inst1_attempts": 0})
	row_second_attempt = SimpleNamespace(**{**vars(_ROW), "inst1_attempts": 1})

	gw1 = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_first_attempt), 1, 1, as_of=_AS_OF, gateway=gw1, store=_StubStore())

	gw2 = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_second_attempt), 1, 1, as_of=_AS_OF, gateway=gw2, store=_StubStore())

	invoice_key1 = next(k for k in gw1.idempotency_keys if k.startswith("settlement-invoice|"))
	invoice_key2 = next(k for k in gw2.idempotency_keys if k.startswith("settlement-invoice|"))
	assert invoice_key1 == invoice_key2


def test_payment_attempt_keys_differ_by_attempt_but_stay_idempotent_within_one():
	"""PR #37 review finding #2 — the actual bug fix: the PAY keys must carry
	the DB-persisted attempt number (never a timestamp), so a genuinely new
	attempt (a client fixing their payment method after a decline) reaches
	Stripe as a fresh request instead of replaying the cached original
	decline, while two identical calls for the SAME attempt still produce the
	SAME key (real idempotency, not a new object every call)."""
	row_attempt_0 = SimpleNamespace(**{**vars(_ROW), "inst1_attempts": 0})
	row_attempt_1 = SimpleNamespace(**{**vars(_ROW), "inst1_attempts": 1})

	gw0 = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_attempt_0), 1, 1, as_of=_AS_OF, gateway=gw0, store=_StubStore())
	pay_key_attempt_0 = next(k for k in gw0.idempotency_keys if k.startswith("settlement-pay-ach|"))

	gw1 = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_attempt_1), 1, 1, as_of=_AS_OF, gateway=gw1, store=_StubStore())
	pay_key_attempt_1 = next(k for k in gw1.idempotency_keys if k.startswith("settlement-pay-ach|"))

	assert pay_key_attempt_0 != pay_key_attempt_1
	assert pay_key_attempt_0.endswith("|r0a0")
	assert pay_key_attempt_1.endswith("|r0a1")

	# Same attempt, called twice (e.g. a duplicate worker execution) — still
	# produces the identical key, so Stripe (or a fake asserting on the key)
	# treats it as the same request, not two.
	gw0_dup = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(SimpleNamespace(**vars(row_attempt_0))), 1, 1, as_of=_AS_OF, gateway=gw0_dup, store=_StubStore())
	pay_key_attempt_0_dup = next(k for k in gw0_dup.idempotency_keys if k.startswith("settlement-pay-ach|"))
	assert pay_key_attempt_0_dup == pay_key_attempt_0


def test_reopened_retry_charges_the_current_payment_method_and_a_fresh_key():
	"""Re-review finding #1/#3: a client fixes their declined ACH payment
	method, an operator reopens the FAILED_PERMANENT installment
	(reopen_failed_permanent_installment resets attempts to 0 but bumps
	reopen_count to 1), and the very next retry must (a) pass the CURRENTLY
	on-file payment method to pay_invoice rather than relying on the invoice's
	stale default_payment_method, and (b) use a pay-attempt idempotency key
	distinct from the original exhausted attempt's `|r0a0`/`|r0a1`/`|r0a2` —
	otherwise Stripe replays the original cached decline within its ~24h
	idempotency-key retention window."""
	row_reopened = SimpleNamespace(
		**{**vars(_ROW), "inst1_attempts": 0, "inst1_reopen_count": 1, "inst1_stripe_invoice_id": "in_existing"},
	)
	gw = _FakeGateway([PayOutcome(status="paid")])
	charge_installment(_FakeSession(row_reopened), 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())

	assert gw.pay_calls == ["enc-ach"]  # the currently-on-file method, not None
	pay_key = next(k for k in gw.idempotency_keys if k.startswith("settlement-pay-ach|"))
	assert pay_key.endswith("|r1a0")
	assert pay_key not in {"settlement-pay-ach|1|1|r0a0", "settlement-pay-ach|1|1|r0a1", "settlement-pay-ach|1|1|r0a2"}


def test_live_gateway_maps_async_ach_pending_to_processing_not_decline():
	"""Re-review verification finding (not in the original two review
	findings — found by testing against real Stripe test mode): Stripe
	NEVER returns a synchronous decline for ACH — every ACH failure arrives
	later via the invoice.payment_failed webhook. invoices.pay() returning
	without raising, with the invoice still status='open', is the NORMAL
	in-flight state, not a decline. Confirmed against a real Stripe test-mode
	ACH payment: pay() returned status='open', and the same invoice reached
	status='paid' moments later with no exception ever raised. Misreading
	that as a decline would fire the card fallback (or mark FAILED) while
	the ACH charge was still genuinely succeeding — a double-charge risk."""
	gw = object.__new__(LiveStripeGateway)
	gw._client = SimpleNamespace(
		invoices=SimpleNamespace(pay=lambda invoice_id, params, options: SimpleNamespace(status="open"))
	)
	outcome = gw.pay_invoice(stripe_invoice_id="in_123", payment_method_id="pm_ach", idempotency_key="k")
	assert outcome.status == "processing"
	assert outcome.error_code is None
	assert not outcome.is_transport_error


def test_live_gateway_maps_synchronous_paid_result_to_paid():
	gw = object.__new__(LiveStripeGateway)
	gw._client = SimpleNamespace(
		invoices=SimpleNamespace(pay=lambda invoice_id, params, options: SimpleNamespace(status="paid"))
	)
	outcome = gw.pay_invoice(stripe_invoice_id="in_123", payment_method_id="pm_card", idempotency_key="k")
	assert outcome.status == "paid"


def test_crash_between_create_invoice_and_savepoint_is_reconciled_not_duplicated():
	"""Re-review finding #2: simulates a worker crash after Stripe accepted
	create_invoice but before the savepoint persisting stripe_invoice_id ever
	committed — the retry's DB row has no stripe_invoice_id at all, exactly as
	if create_invoice had never been called. find_invoice_by_metadata must
	find the already-created invoice (by the same transaction_id/installment
	metadata every invoice this path creates carries) and reuse it, so
	create_invoice is never called a second time."""

	class _ReconcilingGateway(_FakeGateway):
		def __init__(self, *a, **kw):
			super().__init__(*a, **kw)
			self.create_invoice_calls = 0

		def create_invoice(self, **kw):
			self.create_invoice_calls += 1
			return super().create_invoice(**kw)

		def find_invoice_by_metadata(self, *, stripe_customer_id, metadata_filter):
			assert metadata_filter == {"transaction_id": "1", "installment": "1"}
			return InvoiceHandle(stripe_invoice_id="in_from_crash", status="draft")

	row_after_crash = SimpleNamespace(**{**vars(_ROW), "inst1_stripe_invoice_id": None})
	gw = _ReconcilingGateway([PayOutcome(status="paid")])
	outcome = charge_installment(_FakeSession(row_after_crash), 1, 1, as_of=_AS_OF, gateway=gw, store=_StubStore())

	assert outcome.status == "CHARGED"
	assert outcome.stripe_invoice_id == "in_from_crash"
	assert gw.create_invoice_calls == 0
