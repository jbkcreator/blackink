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
from src.services.settlement.ledger import (
	claim_installment_1,
	claim_installment_2,
	open_settlement,
	record_agreement_terminated,
	record_door_signed,
)
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
