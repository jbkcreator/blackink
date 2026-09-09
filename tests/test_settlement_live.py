"""Live-DB tests for the settlement engine (Subtask 1.2.2). Requires a real
Postgres with migrations applied — same class as
tests/test_tenant_isolation.py and tests/test_appointment_ops_live.py.

Covers what a FakeSession cannot: the composite same-tenant FK, the
trigger's fail-closed rejections, RLS on both new tables, DELETE revoked
for both runtime roles, exactly-once billing across multiple appointment
rows sharing one opportunity_id (discharging Subtask 1.1.1's deferred
promise), the non-poach invariance argument for NOT extending
client_pm_books, and fast-forwarding the 60-day clock via an explicit
claim_time/as_of rather than clock mocking.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from src.core.database import get_db_context, get_owner_db_context, get_system_db_context
from src.services.settlement.charge import charge_installment
from src.services.settlement.clawback import void_terminated_installment_2_batch
from src.services.settlement.gateway import InvoiceHandle, PayOutcome, StripeGateway
from src.services.settlement.ledger import (
	claim_installment_1,
	claim_installment_2,
	claim_terminated_installment_2_for_void,
	mark_installment,
	open_settlement,
	record_agreement_terminated,
	record_door_signed,
	reopen_failed_permanent_installment,
)
from src.services.settlement.store import EvidencePacketStore
from tests.fixtures.synthetic_tenants import CANARY_A, CANARY_B, canary_tenants  # noqa: F401

_OFFER_CODE = "_leakcanary_settlement_offer"


@pytest.fixture
def offer(canary_tenants):
	"""A settlement_enabled offer_code, torn down after the test."""
	with get_owner_db_context() as s:
		s.execute(
			text(
				"INSERT INTO settlement_offer_config "
				"(offer_code, settlement_enabled, pricing_basis, per_door_bounty_cents) "
				"VALUES (:offer_code, TRUE, 'PER_DOOR', 10000) "
				"ON CONFLICT (offer_code) DO UPDATE SET settlement_enabled = TRUE"
			),
			{"offer_code": _OFFER_CODE},
		)
	yield _OFFER_CODE
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE offer_code = :o"), {"o": _OFFER_CODE})
		s.execute(text("DELETE FROM settlement_offer_config WHERE offer_code = :o"), {"o": _OFFER_CODE})


@pytest.fixture
def agreement(canary_tenants):
	"""One ACTIVE pms_agreement for CANARY_A, cleaned up after the test."""
	ctx = canary_tenants[CANARY_A]
	opportunity_id = str(uuid.uuid4())
	door_signed_at = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		pms_agreement_id = record_door_signed(
			s, client_id=CANARY_A, opportunity_id=opportunity_id, door_signed_at=door_signed_at,
			agreement_source="PMS_SYNC", company_id=ctx["company_id"], door_count=5,
		)
	yield {
		"pms_agreement_id": pms_agreement_id, "opportunity_id": opportunity_id,
		"door_signed_at": door_signed_at, "company_id": ctx["company_id"],
	}
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})


def test_migrations_are_idempotent():
	# Importing and re-running the migration main() twice must not raise.
	import migrations.apply_pms_agreements as m1
	import migrations.apply_settlement_ledger as m2

	assert m1.main() == 0
	assert m1.main() == 0
	assert m2.main() == 0
	assert m2.main() == 0


def test_open_settlement_creates_row_with_split(offer, agreement):
	with get_system_db_context() as s:
		transaction_id = open_settlement(
			s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], offer_code=offer
		)
		row = s.execute(
			text(
				"SELECT total_bounty_cents, installment_1_cents, installment_2_cents, "
				"       installment_2_scheduled_for, door_signed_at "
				"FROM settlement_transactions WHERE transaction_id = :id"
			),
			{"id": transaction_id},
		).one()
	assert row.total_bounty_cents == 50_000  # 5 doors * 10000
	assert row.installment_1_cents == 25_000
	assert row.installment_2_cents == 25_000
	# DoD: "installment 2 scheduled 60 days out" — a real committed row.
	assert (row.installment_2_scheduled_for - row.door_signed_at) == timedelta(days=60)


def test_duplicate_door_signed_is_a_noop(canary_tenants):
	ctx = canary_tenants[CANARY_A]
	opportunity_id = str(uuid.uuid4())
	door_signed_at = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		first_id = record_door_signed(
			s, client_id=CANARY_A, opportunity_id=opportunity_id, door_signed_at=door_signed_at,
			agreement_source="PMS_SYNC", company_id=ctx["company_id"],
		)
		second_id = record_door_signed(
			s, client_id=CANARY_A, opportunity_id=opportunity_id, door_signed_at=door_signed_at,
			agreement_source="PMS_SYNC", company_id=ctx["company_id"],
		)
	assert first_id is not None
	assert second_id is None
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": first_id})


def test_exactly_once_billing_across_multiple_appointments_sharing_opportunity(offer, agreement, canary_tenants):
	"""Discharges Subtask 1.1.1's deferred promise: many appointment rows can
	share one opportunity_id, but at most one settlement may ever bill it."""
	ctx = canary_tenants[CANARY_A]
	with get_system_db_context() as s:
		# Three appointment rows sharing the agreement's opportunity_id —
		# exactly the reschedule-chain scenario idx_opportunity_dedupe
		# permits at the appointments layer.
		for state in ("RESCHEDULED", "REBOOKED", "ATTENDED"):
			s.execute(
				text(
					"INSERT INTO appointments (client_id, company_id, opportunity_id, state, "
					"scheduled_for, owner_brief_url) "
					"VALUES (:client_id, :company_id, :opp, CAST(:state AS appointment_state_enum), "
					"NOW(), 'https://brief.example/x')"
				),
				{"client_id": CANARY_A, "company_id": ctx["company_id"], "opp": agreement["opportunity_id"], "state": state},
			)
		count = s.execute(
			text("SELECT COUNT(*) FROM appointments WHERE opportunity_id = :opp"),
			{"opp": agreement["opportunity_id"]},
		).scalar()
		assert count == 3

		first = open_settlement(s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], offer_code=offer)
		second = open_settlement(s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], offer_code=offer)
		settlement_count = s.execute(
			text("SELECT COUNT(*) FROM settlement_transactions WHERE opportunity_id = :opp"),
			{"opp": agreement["opportunity_id"]},
		).scalar()

	assert first is not None
	assert second is None
	assert settlement_count == 1

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM appointments WHERE opportunity_id = :opp"), {"opp": agreement["opportunity_id"]})


def test_insert_rejects_terminated_agreement(offer, agreement):
	with get_system_db_context() as s:
		record_agreement_terminated(
			s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"],
			terminated_at=datetime.now(timezone.utc),
		)
		with pytest.raises(DatabaseError):
			open_settlement(s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], offer_code=offer)


def test_insert_rejects_mismatched_door_signed_at(offer, agreement):
	with get_system_db_context() as s:
		with pytest.raises(DatabaseError):
			s.execute(
				text(
					"INSERT INTO settlement_transactions "
					"(client_id, pms_agreement_id, opportunity_id, offer_code, door_count, "
					" total_bounty_cents, installment_1_cents, installment_2_cents, "
					" installment_2_scheduled_for, door_signed_at) "
					"VALUES (:client_id, :pms_agreement_id, :opp, :offer_code, 1, 100, 50, 50, NOW(), NOW())"
				),
				{
					"client_id": CANARY_A, "pms_agreement_id": agreement["pms_agreement_id"],
					"opp": agreement["opportunity_id"], "offer_code": offer,
				},
			)


def test_charging_without_evidence_packet_url_rejected(offer, agreement):
	with get_system_db_context() as s:
		transaction_id = open_settlement(
			s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], offer_code=offer
		)
		with pytest.raises(DatabaseError):
			s.execute(
				text("UPDATE settlement_transactions SET installment_1_status = 'CHARGED', "
					 "installment_1_charged_at = NOW() WHERE transaction_id = :id"),
				{"id": transaction_id},
			)


def test_charging_synthetic_agreement_without_flag_rejected(offer, canary_tenants):
	ctx = canary_tenants[CANARY_A]
	opportunity_id = str(uuid.uuid4())
	door_signed_at = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		pms_agreement_id = record_door_signed(
			s, client_id=CANARY_A, opportunity_id=opportunity_id, door_signed_at=door_signed_at,
			agreement_source="SYNTHETIC", company_id=ctx["company_id"],
		)
		transaction_id = open_settlement(s, client_id=CANARY_A, pms_agreement_id=pms_agreement_id, offer_code=offer)
		s.execute(
			text("UPDATE settlement_transactions SET evidence_packet_url = 'https://x/y.pdf' WHERE transaction_id = :id"),
			{"id": transaction_id},
		)
		with pytest.raises(DatabaseError):
			s.execute(
				text("UPDATE settlement_transactions SET installment_1_status = 'CHARGED', "
					 "installment_1_charged_at = NOW() WHERE transaction_id = :id"),
				{"id": transaction_id},
			)
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})


def test_charged_cannot_transition_away(offer, agreement):
	with get_system_db_context() as s:
		transaction_id = open_settlement(
			s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], offer_code=offer
		)
		s.execute(
			text(
				"UPDATE settlement_transactions SET evidence_packet_url = 'https://x/y.pdf', "
				"installment_1_status = 'CHARGED', installment_1_charged_at = NOW() WHERE transaction_id = :id"
			),
			{"id": transaction_id},
		)
		with pytest.raises(DatabaseError):
			s.execute(
				text("UPDATE settlement_transactions SET installment_1_status = 'FAILED' WHERE transaction_id = :id"),
				{"id": transaction_id},
			)


def test_composite_fk_rejects_cross_tenant_agreement(offer, agreement):
	with get_system_db_context() as s:
		with pytest.raises(DatabaseError):
			s.execute(
				text(
					"INSERT INTO settlement_transactions "
					"(client_id, pms_agreement_id, opportunity_id, offer_code, door_count, "
					" total_bounty_cents, installment_1_cents, installment_2_cents, "
					" installment_2_scheduled_for, door_signed_at) "
					"VALUES (:client_id, :pms_agreement_id, :opp, :offer_code, 1, 100, 50, 50, "
					" :door_signed_at + INTERVAL '60 days', :door_signed_at)"
				),
				{
					"client_id": CANARY_B,  # mismatched tenant
					"pms_agreement_id": agreement["pms_agreement_id"],
					"opp": agreement["opportunity_id"], "offer_code": offer,
					"door_signed_at": agreement["door_signed_at"],
				},
			)


def test_no_delete_grant_on_either_table():
	with get_system_db_context() as s:
		for table, role in (
			("pms_agreements", "blackink_app"), ("pms_agreements", "blackink_system"),
			("settlement_transactions", "blackink_app"), ("settlement_transactions", "blackink_system"),
		):
			has_delete = s.execute(
				text(
					"SELECT has_table_privilege(:role, :table, 'DELETE')"
				),
				{"role": role, "table": table},
			).scalar()
			assert not has_delete, f"{role} must not have DELETE on {table}"


def test_rls_forced_on_both_tables():
	with get_system_db_context() as s:
		for table in ("pms_agreements", "settlement_transactions"):
			row = s.execute(
				text(
					"SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
				),
				{"t": table},
			).one()
			assert row.relrowsecurity is True
			assert row.relforcerowsecurity is True


def test_non_poach_claim_survives_agreement_termination(offer, agreement, canary_tenants):
	"""The regression guard for the migration's core argument: terminating a
	pms_agreement must NEVER touch the client_pm_books non-poach claim."""
	ctx = canary_tenants[CANARY_A]
	domain = ctx["domain"]
	with get_system_db_context() as s:
		extra_pms_agreement_id = record_door_signed(
			s, client_id=CANARY_A, opportunity_id=str(uuid.uuid4()), door_signed_at=datetime.now(timezone.utc),
			agreement_source="PMS_SYNC", company_id=ctx["company_id"], owner_domain=domain,
		)
		claim_before = s.execute(
			text("SELECT COUNT(*) FROM client_pm_books WHERE client_id = :cid AND owner_domain = :d"),
			{"cid": CANARY_A, "d": domain},
		).scalar()
		record_agreement_terminated(
			s, client_id=CANARY_A, pms_agreement_id=agreement["pms_agreement_id"], terminated_at=datetime.now(timezone.utc)
		)
		claim_after = s.execute(
			text("SELECT COUNT(*) FROM client_pm_books WHERE client_id = :cid AND owner_domain = :d"),
			{"cid": CANARY_A, "d": domain},
		).scalar()
	# is_claimed_by_other_client() reads the REQUESTING client from the
	# session's own RLS tenant context (app.current_client_id), not from a
	# parameter — probe it as CANARY_B asking about CANARY_A's company.
	with get_db_context(client_id=CANARY_B) as s:
		is_claimed = s.execute(
			text("SELECT is_claimed_by_other_client(:company_id)"),
			{"company_id": ctx["company_id"]},
		).scalar()
	assert claim_before == 1
	assert claim_after == 1  # untouched by termination
	assert is_claimed is True

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM client_pm_books WHERE client_id = :cid AND owner_domain = :d"), {"cid": CANARY_A, "d": domain})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": extra_pms_agreement_id})


def test_concurrent_claim_installment_2_claims_disjoint_sets(offer, canary_tenants):
	ctx = canary_tenants[CANARY_A]
	past = datetime.now(timezone.utc) - timedelta(days=61)
	ids = []
	with get_system_db_context() as s:
		for _ in range(4):
			pms_agreement_id = record_door_signed(
				s, client_id=CANARY_A, opportunity_id=str(uuid.uuid4()), door_signed_at=past,
				agreement_source="PMS_SYNC", company_id=ctx["company_id"],
			)
			transaction_id = open_settlement(s, client_id=CANARY_A, pms_agreement_id=pms_agreement_id, offer_code=offer)
			ids.append((pms_agreement_id, transaction_id))

	claim_time = datetime.now(timezone.utc)
	with get_system_db_context() as s1, get_system_db_context() as s2:
		claimed1 = claim_installment_2(s1, claim_time=claim_time, limit=2)
		claimed2 = claim_installment_2(s2, claim_time=claim_time, limit=2)
	assert set(claimed1).isdisjoint(claimed2)
	assert len(claimed1) + len(claimed2) <= 4

	with get_owner_db_context() as s:
		for pms_agreement_id, transaction_id in ids:
			s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
			s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})


# ── PR #37 review finding #1: a terminated agreement must never abort a
# mixed claim batch, and its eligible installment 2 must still reach
# VOIDED_CLAWBACK ─────────────────────────────────────────────────────────

class _StubEvidenceStore(EvidencePacketStore):
	def publish(self, **kw):
		return "https://files.stripe.com/live-test.pdf"


class _RecordingGateway(StripeGateway):
	def __init__(self, pay_status: str = "paid"):
		self.calls: list[str] = []
		self._pay_status = pay_status

	def create_invoice(self, **kw):
		self.calls.append("create_invoice")
		return InvoiceHandle(stripe_invoice_id=f"in_{uuid.uuid4().hex[:8]}", status="draft")

	def find_invoice_by_metadata(self, **kw):
		self.calls.append("find_invoice_by_metadata")
		return None

	def retrieve_invoice(self, *, stripe_invoice_id):
		self.calls.append("retrieve_invoice")
		return InvoiceHandle(stripe_invoice_id=stripe_invoice_id, status="draft")

	def add_invoice_item(self, **kw):
		self.calls.append("add_invoice_item")

	def update_invoice_metadata(self, **kw):
		self.calls.append("update_invoice_metadata")

	def finalize_invoice(self, *, stripe_invoice_id, idempotency_key):
		self.calls.append("finalize_invoice")
		return InvoiceHandle(stripe_invoice_id=stripe_invoice_id, status="open")

	def pay_invoice(self, **kw):
		self.calls.append("pay_invoice")
		return PayOutcome(status=self._pay_status, error_code="card_declined", error_message="declined")

	def void_or_delete_invoice(self, **kw):
		self.calls.append("void_or_delete_invoice")


def _open_settlement_for_new_agreement(session, *, client_id, company_id, offer_code, door_signed_at):
	pms_agreement_id = record_door_signed(
		session, client_id=client_id, opportunity_id=str(uuid.uuid4()), door_signed_at=door_signed_at,
		agreement_source="PMS_SYNC", company_id=company_id,
	)
	transaction_id = open_settlement(session, client_id=client_id, pms_agreement_id=pms_agreement_id, offer_code=offer_code)
	return pms_agreement_id, transaction_id


def test_mixed_batch_terminated_agreement_does_not_abort_installment_1_claim(offer, canary_tenants):
	"""PR #37 review finding #1, first half: claim_installment_1's bulk
	UPDATE previously had no agreement-status predicate — a single
	terminated (non-ACTIVE) agreement in the SAME claim batch made the
	guard trigger reject entry into CHARGING for that one row, which aborts
	the whole bulk UPDATE statement, so a healthy sibling transaction in the
	same batch was never claimed either. This must no longer happen: the
	healthy transaction claims normally and the terminated one is excluded,
	not both lost."""
	ctx = canary_tenants[CANARY_A]
	ids = []
	with get_system_db_context() as s:
		healthy_agreement_id, healthy_txn = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=datetime.now(timezone.utc),
		)
		ids.append((healthy_agreement_id, healthy_txn))
		terminated_agreement_id, terminated_txn = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=datetime.now(timezone.utc),
		)
		ids.append((terminated_agreement_id, terminated_txn))
		s.execute(
			text("UPDATE pms_agreements SET status = 'TERMINATED', terminated_at = NOW() WHERE pms_agreement_id = :id"),
			{"id": terminated_agreement_id},
		)

	claim_time = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		claimed = claim_installment_1(s, claim_time=claim_time, limit=10)

	assert healthy_txn in claimed, "the healthy transaction must still be claimable in the same batch"
	assert terminated_txn not in claimed, "a non-ACTIVE agreement's installment 1 must never enter CHARGING"

	with get_owner_db_context() as s:
		for pms_agreement_id, transaction_id in ids:
			s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
			s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})


def test_mixed_batch_terminated_installment_2_voided_healthy_sibling_still_claimed(offer, canary_tenants):
	"""PR #37 review finding #1, second half — the mixed-batch integration
	test required by the review: one healthy installment-2-eligible
	transaction and one whose agreement terminated inside the clawback
	window, claimed in the SAME sweep tick. Before the fix, the terminated
	row's presence in claim_installment_2's own bulk UPDATE made the guard
	trigger reject the whole statement, so the healthy sibling was starved
	too. After the fix: void_terminated_installment_2_batch() drains the
	terminated row to VOIDED_CLAWBACK first, then claim_installment_2 claims
	the healthy row without ever attempting (and failing) on the terminated
	one."""
	ctx = canary_tenants[CANARY_A]
	past = datetime.now(timezone.utc) - timedelta(days=61)
	ids = []
	with get_system_db_context() as s:
		healthy_agreement_id, healthy_txn = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=past,
		)
		ids.append((healthy_agreement_id, healthy_txn))
		terminated_agreement_id, terminated_txn = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=past,
		)
		ids.append((terminated_agreement_id, terminated_txn))
		# Terminated 10 days after signing — well inside the 60-day clawback window.
		s.execute(
			text(
				"UPDATE pms_agreements SET status = 'TERMINATED', terminated_at = :terminated_at "
				"WHERE pms_agreement_id = :id"
			),
			{"id": terminated_agreement_id, "terminated_at": past + timedelta(days=10)},
		)

	claim_time = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		voided = void_terminated_installment_2_batch(s, limit=10)
		claimed = claim_installment_2(s, claim_time=claim_time, limit=10)

	assert voided == 1
	assert healthy_txn in claimed, "the healthy transaction must still be claimable after the terminated row is drained"
	assert terminated_txn not in claimed

	with get_system_db_context() as s:
		status_row = s.execute(
			text("SELECT installment_2_status, is_clawed_back FROM settlement_transactions WHERE transaction_id = :id"),
			{"id": terminated_txn},
		).one()
		events = s.execute(
			text("SELECT event_type FROM events WHERE entity_id = :eid AND event_type = 'settlement_clawback_executed'"),
			{"eid": str(terminated_txn)},
		).all()
	assert status_row.installment_2_status == "VOIDED_CLAWBACK"
	assert status_row.is_clawed_back is True
	assert len(events) == 1

	with get_owner_db_context() as s:
		for pms_agreement_id, transaction_id in ids:
			s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
			s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})


def test_charged_installment_1_is_never_reversed_by_a_later_termination(offer, canary_tenants):
	"""Resolves the task-analysis open question against the source of
	truth: installment 1 (50% at signature) is earned immediately on
	verification and is NEVER clawed back — only installment 2 is subject
	to the 60-day clawback rule. A termination arriving after installment 1
	is already CHARGED must leave it untouched."""
	ctx = canary_tenants[CANARY_A]
	past = datetime.now(timezone.utc) - timedelta(days=5)
	with get_system_db_context() as s:
		pms_agreement_id, transaction_id = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=past,
		)
		# The trigger's evidence-packet CHECK requires evidence_packet_url set
		# BEFORE entering CHARGED — set it first since this test bypasses
		# charge_installment() (which would normally publish it first).
		s.execute(
			text("UPDATE settlement_transactions SET evidence_packet_url = 'https://files.stripe.com/x.pdf' WHERE transaction_id = :id"),
			{"id": transaction_id},
		)
		mark_installment(s, transaction_id, 1, "CHARGED", charged_at=past, stripe_invoice_id="in_already_charged", rail="ACH")
		record_agreement_terminated(s, client_id=CANARY_A, pms_agreement_id=pms_agreement_id, terminated_at=datetime.now(timezone.utc))
		status = s.execute(
			text("SELECT installment_1_status FROM settlement_transactions WHERE transaction_id = :id"),
			{"id": transaction_id},
		).one()
	assert status.installment_1_status == "CHARGED"

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})


# ── PR #37 review finding #2: attempt-numbered payment idempotency +
# exactly-one incident on FAILED_PERMANENT + controlled manual reopening ──

def test_failed_permanent_emits_exactly_one_incident_and_supports_manual_reopen(offer, canary_tenants):
	"""evidence_packet_url is pre-set directly (same convention as the
	fake-session unit tests) so this test exercises only findings #1/#2's
	claim/attempt/incident/reopen logic — NOT assemble_evidence_packet()'s
	real DNC-check query, which needs a `blackink_system` grant on
	compliance_gate_checks that was never added (a genuine but unrelated
	pre-existing gap from Subtask 1.1.1's compliance-gate migration, out of
	this PR's scope — flagged separately, not fixed here)."""
	ctx = canary_tenants[CANARY_A]
	from src.core.token_crypto import encrypt_token
	with get_system_db_context() as s:
		pms_agreement_id, transaction_id = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=datetime.now(timezone.utc),
		)
		s.execute(
			text("UPDATE settlement_transactions SET evidence_packet_url = 'https://files.stripe.com/x.pdf' WHERE transaction_id = :id"),
			{"id": transaction_id},
		)
		s.execute(
			text(
				"UPDATE companies SET stripe_customer_id = 'cus_test', "
				"  ach_payment_method_id_encrypted = NULL, card_payment_method_id_encrypted = :enc "
				"WHERE company_id = :cid"
			),
			{"cid": ctx["company_id"], "enc": encrypt_token("pm_card_test")},
		)

	gw = _RecordingGateway(pay_status="open")  # every attempt declines
	# mark_installment_failed's backoff is 2**attempts minutes — each
	# reclaim below must move claim_time far enough past the PREVIOUS
	# attempt's next_retry_at, or claim_installment_1 correctly (and
	# separately, per its own retry-window contract) refuses to reclaim a
	# row still inside its backoff window.
	claim_time = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		for _ in range(3):
			claimed = claim_installment_1(s, claim_time=claim_time, limit=10)
			assert transaction_id in claimed
			outcome = charge_installment(s, transaction_id, 1, as_of=claim_time, gateway=gw, store=_StubEvidenceStore())
			claim_time = claim_time + timedelta(minutes=10)

	assert outcome.status == "FAILED"
	with get_system_db_context() as s:
		row = s.execute(
			text(
				"SELECT installment_1_status, inst1_attempts, inst1_alerted_permanent_at "
				"FROM settlement_transactions WHERE transaction_id = :id"
			),
			{"id": transaction_id},
		).one()
		incidents = s.execute(
			text(
				"SELECT COUNT(*) AS n FROM events WHERE entity_id = :eid "
				"AND event_type = 'settlement_charge_failed_permanent'"
			),
			{"eid": str(transaction_id)},
		).one()
	assert row.installment_1_status == "FAILED_PERMANENT"
	assert row.inst1_attempts == 3
	assert row.inst1_alerted_permanent_at is not None
	assert incidents.n == 1, "exactly one incident event, not one per attempt"

	# A duplicate/racing call against the SAME already-FAILED_PERMANENT
	# row must never alert a second time.
	with get_system_db_context() as s:
		from src.services.settlement.ledger import mark_installment_failed
		became_permanent_again = mark_installment_failed(s, transaction_id, 1, 3, "declined again")
	assert became_permanent_again is False

	# Controlled manual reopening — the client fixed their payment method.
	with get_system_db_context() as s:
		reopened = reopen_failed_permanent_installment(
			s, transaction_id=transaction_id, installment=1, reason="client updated card on file", as_of=claim_time,
		)
	assert reopened is True

	gw_success = _RecordingGateway(pay_status="paid")
	with get_system_db_context() as s:
		reclaimed = claim_installment_1(s, claim_time=claim_time, limit=10)
		assert transaction_id in reclaimed, "reopened row must be immediately reclaimable"
		final_outcome = charge_installment(s, transaction_id, 1, as_of=claim_time, gateway=gw_success, store=_StubEvidenceStore())
	assert final_outcome.status == "CHARGED"

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})
		s.execute(
			text("UPDATE companies SET stripe_customer_id = NULL, card_payment_method_id_encrypted = NULL WHERE company_id = :cid"),
			{"cid": ctx["company_id"]},
		)


def test_payment_attempt_keys_differ_across_real_claimed_attempts(offer, canary_tenants):
	"""PR #37 review finding #2 against a REAL claim/attempt cycle (not a
	hand-set inst1_attempts like the fake-session unit test): each
	claim_installment_1() call increments inst1_attempts in the database,
	and charge_installment() must derive a genuinely different payment
	idempotency key on each subsequent real attempt while the object-
	creation keys (invoice/item/finalize) stay identical throughout — proven
	by asserting create_invoice is called at most once across all three
	attempts even though pay_invoice is called three times with three
	different keys."""
	ctx = canary_tenants[CANARY_A]
	from src.core.token_crypto import encrypt_token
	with get_system_db_context() as s:
		pms_agreement_id, transaction_id = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx["company_id"], offer_code=offer, door_signed_at=datetime.now(timezone.utc),
		)
		s.execute(
			text("UPDATE settlement_transactions SET evidence_packet_url = 'https://files.stripe.com/x.pdf' WHERE transaction_id = :id"),
			{"id": transaction_id},
		)
		s.execute(
			text(
				"UPDATE companies SET stripe_customer_id = 'cus_test', "
				"  card_payment_method_id_encrypted = :enc, ach_payment_method_id_encrypted = NULL "
				"WHERE company_id = :cid"
			),
			{"cid": ctx["company_id"], "enc": encrypt_token("pm_card_test")},
		)

	claim_time = datetime.now(timezone.utc)
	gw = _RecordingGateway(pay_status="open")
	with get_system_db_context() as s:
		for _ in range(2):
			claim_installment_1(s, claim_time=claim_time, limit=10)
			charge_installment(s, transaction_id, 1, as_of=claim_time, gateway=gw, store=_StubEvidenceStore())
			claim_time = claim_time + timedelta(minutes=10)

	assert gw.calls.count("create_invoice") == 1, "object-creation must happen exactly once across attempts"
	assert gw.calls.count("pay_invoice") == 2, "each real attempt makes a genuine new payment call"

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})
		s.execute(
			text("UPDATE companies SET stripe_customer_id = NULL, card_payment_method_id_encrypted = NULL WHERE company_id = :cid"),
			{"cid": ctx["company_id"]},
		)


# ── Code review finding: tenant isolation for the operator reopen endpoint ─

def test_reopen_cannot_touch_another_tenants_failed_permanent_installment(offer, canary_tenants):
	"""POST /api/v1/settlement/installments/reopen (settlement_router.py)
	scopes its session via get_db_context(client_id=body.client_id) and
	trusts RLS FORCE to make a cross-tenant transaction_id invisible — this
	proves that boundary directly against the same
	reopen_failed_permanent_installment() the router calls, under an
	RLS-scoped session for the WRONG tenant."""
	ctx_a = canary_tenants[CANARY_A]
	with get_system_db_context() as s:
		pms_agreement_id, transaction_id = _open_settlement_for_new_agreement(
			s, client_id=CANARY_A, company_id=ctx_a["company_id"], offer_code=offer, door_signed_at=datetime.now(timezone.utc),
		)
		mark_installment(s, transaction_id, 1, "FAILED_PERMANENT", error="declined")

	# CANARY_B's session must not be able to see, let alone reopen, CANARY_A's transaction.
	with get_db_context(client_id=CANARY_B) as s:
		reopened_cross_tenant = reopen_failed_permanent_installment(
			s, transaction_id=transaction_id, installment=1, reason="wrong-tenant attempt", as_of=datetime.now(timezone.utc),
		)
	assert reopened_cross_tenant is False

	with get_system_db_context() as s:
		status_after_cross_tenant_attempt = s.execute(
			text("SELECT installment_1_status FROM settlement_transactions WHERE transaction_id = :id"),
			{"id": transaction_id},
		).one()
	assert status_after_cross_tenant_attempt.installment_1_status == "FAILED_PERMANENT", (
		"a cross-tenant reopen attempt must never change the row's status"
	)

	# The OWNING tenant's session can reopen it normally.
	with get_db_context(client_id=CANARY_A) as s:
		reopened_same_tenant = reopen_failed_permanent_installment(
			s, transaction_id=transaction_id, installment=1, reason="owning tenant", as_of=datetime.now(timezone.utc),
		)
	assert reopened_same_tenant is True

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM settlement_transactions WHERE transaction_id = :id"), {"id": transaction_id})
		s.execute(text("DELETE FROM pms_agreements WHERE pms_agreement_id = :id"), {"id": pms_agreement_id})
