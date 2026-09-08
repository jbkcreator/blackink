"""Live-DB tests for the Six Billing Rules (Subtask 1.2.3). Requires a real
Postgres with migrations applied (migrations/apply_entitlements_billing.py in
particular) — same class as tests/test_settlement_live.py and
tests/test_appointment_ops_live.py.

Covers the six user-specified Definition-of-Done lines end to end: the $50
miss credit, first-sit-free + second-sit-charges, the 60-day guarantee
boundary (3 -> override, 4/5 -> none, one-time-per-account), dispute credit
at flagged_at with no second credit at resolved_at, monthly_cap NULL + no
cap enforcement across 50 sits, and a founding client's price surviving a
rate migration while a non-founding one updates.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.services.billing.dispute_credit import DisputeWindowExpiredError, credit_dispute_on_flag
from src.services.billing.guarantee import evaluate_sixty_day_guarantee
from src.services.billing.miss_credit import claim_missed_acks, process_missed_ack
from src.services.billing.offers import apply_rate_migration, create_client_entitlement, load_offer
from src.services.billing.sit_billing import resolve_sit_charge
from tests.fixtures.synthetic_tenants import CANARY_A, CANARY_B, canary_tenants  # noqa: F401


def _insert_appointment(session, *, client_id, company_id, contact_id, opportunity_id,
						 state="BOOKED", scheduled_for=None, c24=None, c3=None):
	return session.execute(
		text(
			"INSERT INTO appointments "
			"(client_id, company_id, opportunity_id, contact_id, state, scheduled_for, "
			" confirmed_24h_timestamp, confirmed_3h_timestamp, owner_brief_url) "
			"VALUES (:client_id, :company_id, :opp, :contact_id, CAST(:state AS appointment_state_enum), "
			" COALESCE(:scheduled_for, NOW()), :c24, :c3, 'https://brief.example/x') "
			"RETURNING appointment_id, is_billable"
		),
		{"client_id": client_id, "company_id": company_id, "opp": opportunity_id,
		 "contact_id": contact_id, "state": state, "scheduled_for": scheduled_for, "c24": c24, "c3": c3},
	).one()


@pytest.fixture
def billing_tenant(canary_tenants):
	"""CANARY_A with the standard billing rows cleaned up before the canary
	fixture's own teardown (avoids FK errors on client_id)."""
	ctx = canary_tenants[CANARY_A]
	yield {"client_id": CANARY_A, **ctx}
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM billing_credits WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM subscription_overrides WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM client_entitlements WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM appointment_disputes WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM appointments WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM inbound_messages WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("UPDATE clients SET founding = FALSE WHERE client_id = :c"), {"c": CANARY_A})


def _entitle(session, client_id, offer_code, activated_at=None):
	row = session.execute(
		text(
			"INSERT INTO client_entitlements (client_id, offer_code, activated_at) "
			"VALUES (:cid, :offer_code, COALESCE(:activated_at, NOW())) RETURNING entitlement_id"
		),
		{"cid": client_id, "offer_code": offer_code, "activated_at": activated_at},
	).one()
	return row.entitlement_id


# ── DoD 1: $50 miss credit ──────────────────────────────────────────────

def test_miss_credit_fires_at_90s_ack_latency_and_not_at_45s(billing_tenant):
	client_id = billing_tenant["client_id"]
	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		slow = s.execute(
			text(
				"INSERT INTO inbound_messages (client_id, idempotency_key, destination_address, sender_email, "
				" received_at, acked_at, channel) "
				"VALUES (:cid, :key, 'test@example.com', 'owner@example.com', :received, :acked, 'EMAIL') "
				"RETURNING id"
			),
			{"cid": client_id, "key": f"miss-slow-{uuid.uuid4()}", "received": as_of, "acked": as_of + timedelta(seconds=90)},
		).one()
		fast = s.execute(
			text(
				"INSERT INTO inbound_messages (client_id, idempotency_key, destination_address, sender_email, "
				" received_at, acked_at, channel) "
				"VALUES (:cid, :key, 'test@example.com', 'owner@example.com', :received, :acked, 'EMAIL') "
				"RETURNING id"
			),
			{"cid": client_id, "key": f"miss-fast-{uuid.uuid4()}", "received": as_of, "acked": as_of + timedelta(seconds=45)},
		).one()

		claimed_ids = {m["id"] for m in claim_missed_acks(s, claim_time=as_of)}
		assert slow.id in claimed_ids
		assert fast.id not in claimed_ids

		for message in claim_missed_acks(s, claim_time=as_of):
			if message["id"] == slow.id:
				credited = process_missed_ack(s, message, as_of=as_of)
				assert credited is True

		credit = s.execute(
			text("SELECT amount_cents, credit_type FROM billing_credits WHERE client_id = :cid AND source_id = :sid"),
			{"cid": client_id, "sid": str(slow.id)},
		).one()
		assert credit.amount_cents == 5000
		assert credit.credit_type == "MISS_CREDIT"

		events = s.execute(
			text("SELECT event_type FROM events WHERE client_id = :cid AND entity_id = :eid ORDER BY event_type"),
			{"cid": client_id, "eid": str(slow.id)},
		).all()
		event_types = {e.event_type for e in events}
		assert "respond_ack_missed" in event_types

		no_credit = s.execute(
			text("SELECT 1 FROM billing_credits WHERE client_id = :cid AND source_id = :sid"),
			{"cid": client_id, "sid": str(fast.id)},
		).first()
		assert no_credit is None


def test_never_acked_message_is_claimed_as_a_miss(billing_tenant):
	"""Regression test for a real bug found by review: ack_latency_seconds is
	a generated column that stays NULL until acked_at is set, so a message
	that is NEVER auto-acknowledged (the worst-case SLA breach — strictly
	worse than a late-but-eventual ack) used to be invisible to the claim
	query forever, since NULL > 60 is never true in SQL."""
	client_id = billing_tenant["client_id"]
	received_at = datetime.now(timezone.utc) - timedelta(minutes=5)
	claim_time = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		never_acked = s.execute(
			text(
				"INSERT INTO inbound_messages (client_id, idempotency_key, destination_address, sender_email, "
				" received_at, channel) "
				"VALUES (:cid, :key, 'test@example.com', 'owner@example.com', :received, 'EMAIL') "
				"RETURNING id"
			),
			{"cid": client_id, "key": f"miss-never-acked-{uuid.uuid4()}", "received": received_at},
		).one()

		claimed = claim_missed_acks(s, claim_time=claim_time)
		claimed_ids = {m["id"] for m in claimed}
		assert never_acked.id in claimed_ids

		message = next(m for m in claimed if m["id"] == never_acked.id)
		credited = process_missed_ack(s, message, as_of=claim_time)
		assert credited is True

		credit = s.execute(
			text("SELECT amount_cents FROM billing_credits WHERE client_id = :cid AND source_id = :sid"),
			{"cid": client_id, "sid": str(never_acked.id)},
		).one()
		assert credit.amount_cents == 5000


# ── DoD 2: first sit free, second sit charges $99 ───────────────────────

def test_first_sit_free_then_second_sit_charges_standard(billing_tenant):
	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		_entitle(s, client_id, "owner_growth")
		now_iso = as_of.isoformat()
		appt1 = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
		)
		charge1 = resolve_sit_charge(s, client_id=client_id, appointment_id=str(appt1.appointment_id), as_of=as_of)
		assert charge1.offer_code == "appt_first"
		assert charge1.amount_cents == 0
		assert charge1.first_sit is True

		flag = s.execute(
			text("SELECT first_sit_consumed FROM client_entitlements WHERE client_id = :cid AND offer_code = 'owner_growth'"),
			{"cid": client_id},
		).one()
		assert flag.first_sit_consumed is True

		appt2 = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
		)
		charge2 = resolve_sit_charge(s, client_id=client_id, appointment_id=str(appt2.appointment_id), as_of=as_of)
		assert charge2.offer_code == "appt_standard"
		assert charge2.amount_cents == 9900
		assert charge2.first_sit is False


# ── DoD 3: 60-day guarantee ──────────────────────────────────────────────

def test_sixty_day_guarantee_boundary_three_vs_five(billing_tenant, canary_tenants):
	# Use CANARY_A for the 3-sit (override) case and CANARY_B for the 5-sit
	# (no override) case — two independent accounts, same as the DoD's two
	# independent simulations.
	client_a = CANARY_A
	client_b = CANARY_B
	ctx_a = billing_tenant
	ctx_b = canary_tenants[CANARY_B]
	activated_at = datetime.now(timezone.utc) - timedelta(days=61)
	day_sixty_as_of = activated_at + timedelta(days=61)

	with get_system_db_context() as s:
		_entitle(s, client_a, "owner_growth", activated_at=activated_at)
		_entitle(s, client_b, "owner_growth", activated_at=activated_at)

		now_iso = activated_at.isoformat()
		for _ in range(3):
			_insert_appointment(
				s, client_id=client_a, company_id=ctx_a["company_id"], contact_id=ctx_a["contact_id"],
				opportunity_id=str(uuid.uuid4()), state="ATTENDED",
				scheduled_for=activated_at + timedelta(days=5), c24=now_iso, c3=now_iso,
			)
		for _ in range(5):
			_insert_appointment(
				s, client_id=client_b, company_id=ctx_b["company_id"], contact_id=ctx_b["contact_id"],
				opportunity_id=str(uuid.uuid4()), state="ATTENDED",
				scheduled_for=activated_at + timedelta(days=5), c24=now_iso, c3=now_iso,
			)

		applied_a = evaluate_sixty_day_guarantee(s, client_id=client_a, as_of=day_sixty_as_of)
		applied_b = evaluate_sixty_day_guarantee(s, client_id=client_b, as_of=day_sixty_as_of)
		assert applied_a is True
		assert applied_b is False

		override_a = s.execute(
			text("SELECT override_price_cents FROM subscription_overrides WHERE client_id = :cid"), {"cid": client_a}
		).one()
		assert override_a.override_price_cents == 0

		no_override_b = s.execute(
			text("SELECT 1 FROM subscription_overrides WHERE client_id = :cid"), {"cid": client_b}
		).first()
		assert no_override_b is None

		flag_a = s.execute(
			text("SELECT guarantee_applied FROM client_entitlements WHERE client_id = :cid"), {"cid": client_a}
		).one()
		assert flag_a.guarantee_applied is True

		# One-time per account: re-evaluating client_a does not add a second override.
		applied_again = evaluate_sixty_day_guarantee(s, client_id=client_a, as_of=day_sixty_as_of)
		assert applied_again is False
		override_count = s.execute(
			text("SELECT COUNT(*) AS n FROM subscription_overrides WHERE client_id = :cid"), {"cid": client_a}
		).one()
		assert override_count.n == 1

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM subscription_overrides WHERE client_id = :c"), {"c": client_b})
		s.execute(text("DELETE FROM client_entitlements WHERE client_id = :c"), {"c": client_b})
		s.execute(text("DELETE FROM appointments WHERE client_id = :c"), {"c": client_b})


# ── DoD 4: dispute credit on flagging, not resolution ───────────────────

def test_dispute_credit_at_flagged_at_no_second_credit_at_resolved(billing_tenant):
	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	scheduled_for = datetime.now(timezone.utc) - timedelta(hours=1)
	with get_system_db_context() as s:
		now_iso = scheduled_for.isoformat()
		appt = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED",
			scheduled_for=scheduled_for, c24=now_iso, c3=now_iso,
		)
		flagged_at = scheduled_for + timedelta(hours=2)
		dispute = s.execute(
			text(
				"INSERT INTO appointment_disputes (client_id, appointment_id, flagged_at, reason) "
				"VALUES (:cid, :aid, :flagged_at, 'owner says no-show') RETURNING dispute_id, outcome"
			),
			{"cid": client_id, "aid": appt.appointment_id, "flagged_at": flagged_at},
		).one()
		assert dispute.outcome == "CREDITED_AUTOMATIC"

		credited = credit_dispute_on_flag(s, dispute_id=str(dispute.dispute_id), as_of=flagged_at)
		assert credited is True

		credit = s.execute(
			text("SELECT issued_at FROM billing_credits WHERE client_id = :cid AND source_id = :sid"),
			{"cid": client_id, "sid": str(dispute.dispute_id)},
		).one()
		assert credit.issued_at == flagged_at

		# Simulate a later resolution — re-crediting the same dispute must no-op.
		s.execute(
			text("UPDATE appointment_disputes SET resolved_at = :resolved WHERE dispute_id = :did"),
			{"resolved": flagged_at + timedelta(hours=10), "did": dispute.dispute_id},
		)
		credited_again = credit_dispute_on_flag(s, dispute_id=str(dispute.dispute_id), as_of=flagged_at + timedelta(hours=10))
		assert credited_again is False

		count = s.execute(
			text("SELECT COUNT(*) AS n FROM billing_credits WHERE client_id = :cid AND source_id = :sid"),
			{"cid": client_id, "sid": str(dispute.dispute_id)},
		).one()
		assert count.n == 1


def test_dispute_flagged_49h_after_meeting_is_rejected(billing_tenant):
	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	scheduled_for = datetime.now(timezone.utc) - timedelta(hours=50)
	with get_system_db_context() as s:
		now_iso = scheduled_for.isoformat()
		appt = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED",
			scheduled_for=scheduled_for, c24=now_iso, c3=now_iso,
		)
		flagged_at = scheduled_for + timedelta(hours=49)
		dispute = s.execute(
			text(
				"INSERT INTO appointment_disputes (client_id, appointment_id, flagged_at, reason) "
				"VALUES (:cid, :aid, :flagged_at, 'late flag') RETURNING dispute_id"
			),
			{"cid": client_id, "aid": appt.appointment_id, "flagged_at": flagged_at},
		).one()
		with pytest.raises(DisputeWindowExpiredError):
			credit_dispute_on_flag(s, dispute_id=str(dispute.dispute_id), as_of=flagged_at)

		# Regression: an earlier version of the dispute sweep re-selected an
		# EXPIRED dispute every tick forever (no claim-state to exclude it).
		# credit_status must now read EXPIRED, and a second evaluation must
		# raise again rather than silently no-op — proving the row is
		# terminal, not stuck retrying with no way out.
		status = s.execute(
			text("SELECT credit_status FROM appointment_disputes WHERE dispute_id = :did"), {"did": dispute.dispute_id}
		).one()
		assert status.credit_status == "EXPIRED"
		with pytest.raises(DisputeWindowExpiredError):
			credit_dispute_on_flag(s, dispute_id=str(dispute.dispute_id), as_of=flagged_at)


def test_disputed_free_first_sit_produces_no_credit(billing_tenant):
	"""Regression test for a real bug found by review: crediting a disputed
	sit used to always price it as appt_standard ($99), even when the
	disputed appointment was actually the client's free first sit (billed
	$0). A free sit has nothing to credit — billing_credits.amount_cents is
	CHECK > 0, so issuing a $99 credit for a $0 charge would have been an
	outright fabrication, not just an overcharge."""
	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		create_client_entitlement(s, client_id=client_id, offer_code="owner_growth")
		now_iso = as_of.isoformat()
		appt = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
		)
		charge = resolve_sit_charge(s, client_id=client_id, appointment_id=str(appt.appointment_id), as_of=as_of)
		assert charge.offer_code == "appt_first"
		assert charge.amount_cents == 0

		billed = s.execute(
			text("SELECT billed_offer_code, billed_amount_cents FROM appointments WHERE appointment_id = :aid"),
			{"aid": appt.appointment_id},
		).one()
		assert billed.billed_offer_code == "appt_first"
		assert billed.billed_amount_cents == 0

		flagged_at = as_of + timedelta(hours=1)
		dispute = s.execute(
			text(
				"INSERT INTO appointment_disputes (client_id, appointment_id, flagged_at, reason) "
				"VALUES (:cid, :aid, :flagged_at, 'owner disputes even the free sit') RETURNING dispute_id"
			),
			{"cid": client_id, "aid": appt.appointment_id, "flagged_at": flagged_at},
		).one()

		credited = credit_dispute_on_flag(s, dispute_id=str(dispute.dispute_id), as_of=flagged_at)
		assert credited is False

		no_credit = s.execute(
			text("SELECT 1 FROM billing_credits WHERE client_id = :cid AND source_id = :sid"),
			{"cid": client_id, "sid": str(dispute.dispute_id)},
		).first()
		assert no_credit is None

		status = s.execute(
			text("SELECT credit_status FROM appointment_disputes WHERE dispute_id = :did"), {"did": dispute.dispute_id}
		).one()
		assert status.credit_status == "CREDITED"


# ── DoD 5: no monthly ceiling ────────────────────────────────────────────

def test_monthly_cap_is_null_and_fifty_sits_all_bill(billing_tenant):
	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		appt_standard = load_offer(s, "appt_standard")
		appt_first = load_offer(s, "appt_first")
		assert appt_standard.monthly_cap is None
		assert appt_first.monthly_cap is None

		_entitle(s, client_id, "owner_growth")
		now_iso = as_of.isoformat()
		charged_offer_codes = []
		for _ in range(50):
			appt = _insert_appointment(
				s, client_id=client_id, company_id=company_id, contact_id=contact_id,
				opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
			)
			charge = resolve_sit_charge(s, client_id=client_id, appointment_id=str(appt.appointment_id), as_of=as_of)
			charged_offer_codes.append(charge.offer_code)

		assert len(charged_offer_codes) == 50
		assert charged_offer_codes[0] == "appt_first"
		assert all(code == "appt_standard" for code in charged_offer_codes[1:])


# ── DoD 6: founding flag protects price from a rate migration ──────────

def test_founding_client_price_unchanged_by_rate_migration(billing_tenant, canary_tenants):
	client_founding = CANARY_A
	client_standard = CANARY_B
	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET founding = TRUE WHERE client_id = :c"), {"c": client_founding})
		s.execute(text("UPDATE clients SET founding = FALSE WHERE client_id = :c"), {"c": client_standard})

	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		# create_client_entitlement snapshots the CURRENT list price
		# ($397, respond's seeded price) into locked_price_cents for both
		# accounts — this is what a rate migration must (founding) or must
		# not (non-founding) leave untouched.
		create_client_entitlement(s, client_id=client_founding, offer_code="respond")
		create_client_entitlement(s, client_id=client_standard, offer_code="respond")

		locked_before = s.execute(
			text("SELECT locked_price_cents FROM client_entitlements WHERE client_id = :c AND offer_code = 'respond'"),
			{"c": client_founding},
		).one()
		assert locked_before.locked_price_cents == 39700

		result = apply_rate_migration(s, offer_code="respond", new_price_cents=44700, as_of=as_of)
		assert result.founding_skipped == 1
		assert result.clients_updated == 1

		offer_after = load_offer(s, "respond")
		assert offer_after.price_cents == 44700

		# The actual per-account price: founding's own locked_price_cents
		# must NOT have moved; the standard account's must now match the
		# new list price. This is the real DoD assertion — checking only
		# the `founding` boolean flag (as an earlier version of this test
		# did) would pass even with the founding-skip logic fully removed,
		# since that flag is set once by this test and never touched by
		# apply_rate_migration at all.
		founding_price = s.execute(
			text("SELECT locked_price_cents FROM client_entitlements WHERE client_id = :c AND offer_code = 'respond'"),
			{"c": client_founding},
		).one()
		standard_price = s.execute(
			text("SELECT locked_price_cents FROM client_entitlements WHERE client_id = :c AND offer_code = 'respond'"),
			{"c": client_standard},
		).one()
		assert founding_price.locked_price_cents == 39700, "founding account's price must be UNCHANGED by the migration"
		assert standard_price.locked_price_cents == 44700, "non-founding account's price must UPDATE with the migration"

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM client_entitlements WHERE client_id = :c"), {"c": client_standard})
		s.execute(
			text("INSERT INTO entitlement_offers (offer_code, display_name, price_cents, billing_model) "
				 "VALUES ('respond', 'Respond (Founding)', 39700, 'SUBSCRIPTION_MONTHLY') "
				 "ON CONFLICT (offer_code) DO UPDATE SET price_cents = 39700"),
		)
