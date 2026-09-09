"""Pure tests for charge_sit_for_appointment() (PR #37 review — blocking
findings 1/2: resolve_sit_charge()/apply_pending_credits_to_invoice() were
never wired into any production path; findings #4 and #8 from the second
review round: NO_STRIPE_CUSTOMER must be reclaimable and a mid-sequence
Stripe exception must not risk a duplicate invoice on retry). No DB — a fake
Session/gateway."""
from datetime import datetime, timezone

import pytest

from src.services.billing.sit_invoice import charge_sit_for_appointment


class _Row:
	def __init__(self, **kw):
		kw.setdefault("stripe_invoice_id", None)
		kw.setdefault("sit_invoice_finalized_at", None)
		self.__dict__.update(kw)


class _FakeGateway:
	def __init__(self, *, fail_on: str | None = None):
		self.calls = []
		self._fail_on = fail_on

	def find_invoice_by_metadata(self, **kw):
		self.calls.append(("find_invoice_by_metadata", kw))
		return None

	def create_invoice(self, **kw):
		self.calls.append(("create_invoice", kw))
		if self._fail_on == "create_invoice":
			raise RuntimeError("simulated Stripe transport error")
		return type("H", (), {"stripe_invoice_id": "in_fake", "status": "draft"})()

	def add_invoice_item(self, **kw):
		self.calls.append(("add_invoice_item", kw))
		if self._fail_on == "add_invoice_item":
			raise RuntimeError("simulated Stripe transport error")
		return "ii_fake"

	def finalize_invoice(self, **kw):
		self.calls.append(("finalize_invoice", kw))
		return type("H", (), {"stripe_invoice_id": "in_fake", "status": "open"})()


class _FakeSession:
	def __init__(self, appointment_row):
		self._appointment_row = appointment_row
		self.updates = []
		self.savepoint_depth = 0

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "JOIN clients c" in sql:
			row = self._appointment_row
			return type("R", (), {"first": lambda _self=None, _row=row: _row})()
		if "UPDATE appointments SET billing_blocked_reason = 'NO_STRIPE_CUSTOMER'" in sql:
			self.updates.append(("block", params))
			self._appointment_row.billing_blocked_reason = "NO_STRIPE_CUSTOMER"
			return None
		if "UPDATE appointments SET stripe_invoice_id" in sql:
			self.updates.append(("set_invoice_id", params))
			self._appointment_row.stripe_invoice_id = params["invoice_id"]
			return None
		if "UPDATE appointments SET sit_invoice_finalized_at" in sql:
			self.updates.append(("finalize", params))
			self._appointment_row.sit_invoice_finalized_at = params["as_of"]
			self._appointment_row.billing_blocked_reason = None
			return None
		return type("R", (), {"first": lambda self=None: None, "all": lambda self=None: []})()

	def begin_nested(self):
		from contextlib import contextmanager

		@contextmanager
		def _cm():
			self.savepoint_depth += 1
			try:
				yield
			finally:
				self.savepoint_depth -= 1

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
	assert session.updates[0] == ("block", {"client_id": "acme_pm", "appointment_id": "appt-1"})


def test_already_invoiced_appointment_makes_no_new_stripe_calls():
	"""PR #37 review finding — retrying the same appointment must never
	create a second Stripe invoice, even though resolve_sit_charge() itself
	is idempotent and would return the same charge again."""
	row = _Row(
		is_billable=True, billed_offer_code="appt_first", billed_amount_cents=0, stripe_customer_id="cus_1",
		sit_invoice_finalized_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
	)
	session = _FakeSession(row)
	gw = _FakeGateway()
	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
	)
	assert outcome.status == "ALREADY_INVOICED"
	assert gw.calls == []


def test_billed_but_not_yet_finalized_resumes_instead_of_short_circuiting(monkeypatch):
	"""PR #37 second review finding #8's deeper half: billed_offer_code alone
	must NOT short-circuit to ALREADY_INVOICED — only billed_offer_code AND
	sit_invoice_finalized_at together mean there's nothing left to do. A row
	billed but never finalized (a crash between resolve_sit_charge() and
	finalize_invoice()) must resume and actually reach Stripe."""
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.resolve_sit_charge",
		lambda *a, **kw: type("C", (), {"offer_code": "appt_standard", "amount_cents": 9900, "first_sit": False})(),
	)
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.apply_pending_credits_to_invoice",
		lambda *a, **kw: 0,
	)
	row = _Row(
		is_billable=True, billed_offer_code="appt_standard", billed_amount_cents=9900,
		stripe_customer_id="cus_1", stripe_invoice_id="in_fake", sit_invoice_finalized_at=None,
	)
	session = _FakeSession(row)
	gw = _FakeGateway()
	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
	)
	assert outcome.status == "INVOICED"
	assert [name for name, _ in gw.calls] == ["add_invoice_item", "finalize_invoice"]


def test_success_clears_a_stale_no_stripe_customer_block(monkeypatch):
	"""PR #37 review finding #4 (unit-level half): once an appointment
	actually reaches a real invoice, billing_blocked_reason must not be left
	reading NO_STRIPE_CUSTOMER on a row that just billed fine — the SQL-level
	reclaim itself is covered by the live test in tests/test_billing_live.py."""
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.resolve_sit_charge",
		lambda *a, **kw: type("C", (), {"offer_code": "appt_standard", "amount_cents": 9900, "first_sit": False})(),
	)
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.apply_pending_credits_to_invoice",
		lambda *a, **kw: 0,
	)
	row = _Row(
		is_billable=True, billed_offer_code=None, billed_amount_cents=None,
		stripe_customer_id="cus_1", billing_blocked_reason="NO_STRIPE_CUSTOMER",
	)
	session = _FakeSession(row)
	gw = _FakeGateway()
	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
	)
	assert outcome.status == "INVOICED"
	assert row.billing_blocked_reason is None
	assert row.sit_invoice_finalized_at is not None


def test_mid_sequence_failure_persists_invoice_id_before_raising(monkeypatch):
	"""PR #37 review finding #8: create_invoice succeeds, but the very next
	Stripe call (add_invoice_item) raises. The invoice id must already be
	durably recorded on the appointment (via its own, already-exited
	savepoint) so a retry can resume against the SAME invoice rather than
	risk creating a second one."""
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.resolve_sit_charge",
		lambda *a, **kw: type("C", (), {"offer_code": "appt_standard", "amount_cents": 9900, "first_sit": False})(),
	)
	row = _Row(is_billable=True, billed_offer_code=None, billed_amount_cents=None, stripe_customer_id="cus_1")
	session = _FakeSession(row)
	gw = _FakeGateway(fail_on="add_invoice_item")

	with pytest.raises(RuntimeError):
		charge_sit_for_appointment(
			session, client_id="acme_pm", appointment_id="appt-1",
			as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
		)

	assert row.stripe_invoice_id == "in_fake"
	assert ("set_invoice_id", {"invoice_id": "in_fake", "client_id": "acme_pm", "appointment_id": "appt-1"}) in session.updates


def test_retry_after_mid_sequence_failure_resumes_same_invoice_no_second_create(monkeypatch):
	"""Second half of the finding #8 test: once stripe_invoice_id is already
	set on the row (simulating the retry after the failure above), the next
	call must NOT call create_invoice again."""
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.resolve_sit_charge",
		lambda *a, **kw: type("C", (), {"offer_code": "appt_standard", "amount_cents": 9900, "first_sit": False})(),
	)
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.apply_pending_credits_to_invoice",
		lambda *a, **kw: 0,
	)
	row = _Row(
		is_billable=True, billed_offer_code=None, billed_amount_cents=None,
		stripe_customer_id="cus_1", stripe_invoice_id="in_fake",
	)
	session = _FakeSession(row)
	gw = _FakeGateway()

	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
	)

	assert outcome.status == "INVOICED"
	assert outcome.stripe_invoice_id == "in_fake"
	assert [name for name, _ in gw.calls] == ["add_invoice_item", "finalize_invoice"]


def test_crash_before_savepoint_commits_is_reconciled_not_duplicated(monkeypatch):
	"""Re-review finding #2: simulates a worker crash after Stripe accepted
	create_invoice but before the savepoint persisting stripe_invoice_id ever
	committed to disk — the retry's row has stripe_invoice_id=None, exactly as
	if create_invoice had never been called. find_invoice_by_metadata must
	find the already-created invoice by the (appointment_id, purpose) metadata
	this path always stamps, and reuse it rather than creating a second one."""
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.resolve_sit_charge",
		lambda *a, **kw: type("C", (), {"offer_code": "appt_standard", "amount_cents": 9900, "first_sit": False})(),
	)
	monkeypatch.setattr(
		"src.services.billing.sit_invoice.apply_pending_credits_to_invoice",
		lambda *a, **kw: 0,
	)
	row = _Row(is_billable=True, billed_offer_code=None, billed_amount_cents=None, stripe_customer_id="cus_1")
	session = _FakeSession(row)

	class _ReconcilingGateway(_FakeGateway):
		def find_invoice_by_metadata(self, *, stripe_customer_id, metadata_filter):
			assert metadata_filter == {"appointment_id": "appt-1", "purpose": "sit_charge"}
			return type("H", (), {"stripe_invoice_id": "in_from_crash", "status": "draft"})()

	gw = _ReconcilingGateway()
	outcome = charge_sit_for_appointment(
		session, client_id="acme_pm", appointment_id="appt-1",
		as_of=datetime(2026, 9, 1, tzinfo=timezone.utc), gateway=gw,
	)

	assert outcome.status == "INVOICED"
	assert outcome.stripe_invoice_id == "in_from_crash"
	assert [name for name, _ in gw.calls] == ["add_invoice_item", "finalize_invoice"]
