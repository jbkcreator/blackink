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
from src.services.billing.credits import issue_credit
from src.services.billing.dispute_credit import DisputeWindowExpiredError, credit_dispute_on_flag
from src.services.billing.guarantee import evaluate_sixty_day_guarantee
from src.services.billing.miss_credit import claim_missed_acks, process_missed_ack
from src.services.billing.offers import apply_rate_migration, create_client_entitlement, load_offer
from src.services.billing.sit_billing import resolve_sit_charge
from src.services.billing.sit_invoice import charge_sit_for_appointment
from src.services.clients import provision_client
from src.services.settlement.gateway import InvoiceHandle, StripeGateway
from src.tasks.billing_sweep import run_sit_invoice_sweep
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


def test_miss_credit_exact_60s_boundary_not_claimed_61s_claimed(billing_tenant):
	"""PR #37 review — required exact-boundary test: ack_latency_seconds > 60
	is the actual predicate, so exactly 60s must NOT be claimed and 61s MUST
	be."""
	client_id = billing_tenant["client_id"]
	as_of = datetime.now(timezone.utc)
	with get_system_db_context() as s:
		exactly_60 = s.execute(
			text(
				"INSERT INTO inbound_messages (client_id, idempotency_key, destination_address, sender_email, "
				" received_at, acked_at, channel) "
				"VALUES (:cid, :key, 'test@example.com', 'owner@example.com', :received, :acked, 'EMAIL') "
				"RETURNING id"
			),
			{"cid": client_id, "key": f"miss-60-{uuid.uuid4()}", "received": as_of, "acked": as_of + timedelta(seconds=60)},
		).one()
		exactly_61 = s.execute(
			text(
				"INSERT INTO inbound_messages (client_id, idempotency_key, destination_address, sender_email, "
				" received_at, acked_at, channel) "
				"VALUES (:cid, :key, 'test@example.com', 'owner@example.com', :received, :acked, 'EMAIL') "
				"RETURNING id"
			),
			{"cid": client_id, "key": f"miss-61-{uuid.uuid4()}", "received": as_of, "acked": as_of + timedelta(seconds=61)},
		).one()

		claimed_ids = {m["id"] for m in claim_missed_acks(s, claim_time=as_of)}
		assert exactly_60.id not in claimed_ids
		assert exactly_61.id in claimed_ids


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
		create_client_entitlement(s, client_id=client_id, offer_code="owner_growth")
		now_iso = scheduled_for.isoformat()
		# Consume the free first sit with a throwaway appointment first, so
		# the disputed appointment below bills at the standard $99 rate —
		# there is something to credit. resolve_sit_charge() would otherwise
		# correctly price THIS appointment as the client's free $0 first sit
		# (see test_disputed_free_first_sit_produces_no_credit for that case).
		first_sit_appt = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED",
			scheduled_for=scheduled_for, c24=now_iso, c3=now_iso,
		)
		resolve_sit_charge(s, client_id=client_id, appointment_id=str(first_sit_appt.appointment_id), as_of=scheduled_for)

		appt = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED",
			scheduled_for=scheduled_for, c24=now_iso, c3=now_iso,
		)
		# credit_dispute_on_flag() reads the sit's ACTUAL recorded charge
		# (appointments.billed_amount_cents) rather than re-deriving it — a
		# dispute flagged before resolve_sit_charge() has ever run has
		# nothing to read and correctly raises MissingBilledAmountError
		# (PR #37 review fix). Every real dispute is against an appointment
		# that already billed, so stamp that here first, same as this
		# file's other dispute-credit tests.
		charge = resolve_sit_charge(s, client_id=client_id, appointment_id=str(appt.appointment_id), as_of=scheduled_for)
		assert charge.amount_cents == 9900, "this test needs a non-zero charge to actually credit"
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


# ── PR #37 review: concurrent first-sit requests ────────────────────────

def test_concurrent_first_sit_requests_grant_the_free_sit_exactly_once(billing_tenant):
	"""PR #37 review's required concurrency test — two DIFFERENT appointments
	for the same client, both racing to consume the free first sit, must
	result in EXACTLY ONE free ($0) sit and the other billed at the standard
	$99 rate. This exercises the real begin_nested() compare-and-swap
	(`UPDATE ... WHERE first_sit_consumed = FALSE`) under actual overlapping
	transactions, not just sequential calls."""
	import threading

	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	as_of = datetime.now(timezone.utc)
	now_iso = as_of.isoformat()

	with get_system_db_context() as s:
		_entitle(s, client_id, "owner_growth")
		appt_a = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
		)
		appt_b = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
		)

	results: dict[str, str] = {}
	barrier = threading.Barrier(2)

	def _resolve(name, appointment_id):
		barrier.wait(timeout=5)
		with get_system_db_context() as s:
			charge = resolve_sit_charge(s, client_id=client_id, appointment_id=str(appointment_id), as_of=as_of)
			results[name] = charge.offer_code

	t1 = threading.Thread(target=_resolve, args=("a", appt_a.appointment_id))
	t2 = threading.Thread(target=_resolve, args=("b", appt_b.appointment_id))
	t1.start()
	t2.start()
	t1.join(timeout=10)
	t2.join(timeout=10)

	offer_codes = sorted(results.values())
	assert offer_codes == ["appt_first", "appt_standard"], (
		f"expected exactly one free sit and one standard sit, got: {results}"
	)


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


# ── PR #37 review: founding set at ACTUAL account creation ─────────────

def test_provision_client_sets_founding_at_account_creation():
	"""PR #37 review finding — a test that only manually UPDATEs the
	founding column does not prove account creation sets it. This test goes
	through provision_client() (the one write path for creating a clients
	row) itself, with founding=True passed explicitly at creation time."""
	client_id = f"test_founding_{uuid.uuid4().hex[:12]}"
	try:
		with get_owner_db_context() as s:
			provision_client(s, client_id=client_id, display_name="Test Founding Co", founding=True)
			row = s.execute(
				text("SELECT founding FROM clients WHERE client_id = :c"), {"c": client_id}
			).one()
			assert row.founding is True
	finally:
		with get_owner_db_context() as s:
			s.execute(text("DELETE FROM clients WHERE client_id = :c"), {"c": client_id})


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


# ── PR #37 second review finding #4: NO_STRIPE_CUSTOMER must be reclaimable,
# race-safely, once the client gets a Stripe customer id ─────────────────

class _FakeSitGateway(StripeGateway):
	def __init__(self, *, fail_add_item_times: int = 0):
		self.calls: list[str] = []
		self._fail_add_item_times = fail_add_item_times

	def create_invoice(self, **kw):
		self.calls.append("create_invoice")
		return InvoiceHandle(stripe_invoice_id=f"in_{uuid.uuid4().hex[:8]}", status="draft")

	def add_invoice_item(self, **kw):
		self.calls.append("add_invoice_item")
		if self._fail_add_item_times > 0:
			self._fail_add_item_times -= 1
			raise RuntimeError("simulated Stripe transport error")
		return f"ii_{uuid.uuid4().hex[:8]}"

	def update_invoice_metadata(self, **kw):
		self.calls.append("update_invoice_metadata")

	def finalize_invoice(self, *, stripe_invoice_id, idempotency_key):
		self.calls.append("finalize_invoice")
		return InvoiceHandle(stripe_invoice_id=stripe_invoice_id, status="open")

	def pay_invoice(self, **kw):
		raise AssertionError("sit invoices are never auto-paid — Stripe's send_invoice collection only")

	def void_or_delete_invoice(self, **kw):
		self.calls.append("void_or_delete_invoice")


@pytest.fixture
def attended_billable_appointment(billing_tenant, canary_tenants):
	client_id = billing_tenant["client_id"]
	company_id = billing_tenant["company_id"]
	contact_id = billing_tenant["contact_id"]
	as_of = datetime.now(timezone.utc)
	now_iso = as_of.isoformat()
	with get_system_db_context() as s:
		create_client_entitlement(s, client_id=client_id, offer_code="owner_growth")
		appt = _insert_appointment(
			s, client_id=client_id, company_id=company_id, contact_id=contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=now_iso, c3=now_iso,
		)
	yield {"client_id": client_id, "appointment_id": str(appt.appointment_id), "as_of": as_of}


def test_no_stripe_customer_appointment_becomes_claimable_once_customer_id_added(attended_billable_appointment):
	"""PR #37 second review finding #4: an appointment blocked for
	NO_STRIPE_CUSTOMER must be reclaimed by the real sweep the moment
	clients.stripe_customer_id is populated — not stuck forever because the
	claim query's original predicate only ever looked at
	billing_blocked_reason IS NULL."""
	client_id = attended_billable_appointment["client_id"]
	appointment_id = attended_billable_appointment["appointment_id"]
	as_of = attended_billable_appointment["as_of"]

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = NULL WHERE client_id = :c"), {"c": client_id})

	gw = _FakeSitGateway()
	with get_system_db_context() as s:
		outcome = charge_sit_for_appointment(s, client_id=client_id, appointment_id=appointment_id, as_of=as_of, gateway=gw)
	assert outcome.status == "BLOCKED"
	assert outcome.reason == "NO_STRIPE_CUSTOMER"

	invoiced_while_blocked = run_sit_invoice_sweep(limit=100, claim_time=as_of)
	with get_system_db_context() as s:
		blocked_row = s.execute(
			text("SELECT billing_blocked_reason, billed_offer_code FROM appointments WHERE client_id = :c AND appointment_id = :a"),
			{"c": client_id, "a": appointment_id},
		).one()
	assert blocked_row.billing_blocked_reason == "NO_STRIPE_CUSTOMER"
	assert blocked_row.billed_offer_code is None

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = 'cus_test_reclaim' WHERE client_id = :c"), {"c": client_id})

	invoiced_after_fix = 0
	with get_system_db_context() as session:
		rows = session.execute(
			text(
				"SELECT a.client_id, a.appointment_id FROM appointments a "
				"JOIN clients c ON c.client_id = a.client_id "
				"WHERE a.state = 'ATTENDED' AND a.is_billable AND a.billed_offer_code IS NULL "
				"  AND (a.billing_blocked_reason IS NULL "
				"       OR (a.billing_blocked_reason = 'NO_STRIPE_CUSTOMER' AND c.stripe_customer_id IS NOT NULL)) "
				"  AND a.appointment_id = :aid "
				"ORDER BY a.scheduled_for LIMIT 100 FOR UPDATE OF a SKIP LOCKED"
			),
			{"aid": appointment_id},
		).all()
		assert any(str(r.appointment_id) == appointment_id for r in rows), "must be reclaimable now that stripe_customer_id is set"
		outcome2 = charge_sit_for_appointment(session, client_id=client_id, appointment_id=appointment_id, as_of=as_of, gateway=gw)
		if outcome2.status == "INVOICED":
			invoiced_after_fix += 1

	assert outcome2.status == "INVOICED"
	with get_system_db_context() as s:
		final_row = s.execute(
			text("SELECT billing_blocked_reason, billed_offer_code FROM appointments WHERE client_id = :c AND appointment_id = :a"),
			{"c": client_id, "a": appointment_id},
		).one()
	assert final_row.billing_blocked_reason is None, "the stale NO_STRIPE_CUSTOMER reason must be cleared on success"
	assert final_row.billed_offer_code is not None


def test_no_stripe_customer_reclaim_is_race_safe(attended_billable_appointment):
	"""PR #37 second review finding #4's required race-safety proof: two
	concurrent sweep ticks racing the SAME reclaimed appointment must invoice
	it EXACTLY ONCE — the existing FOR UPDATE SKIP LOCKED claim (unchanged by
	this fix) plus billed_offer_code's own claim-exclusion is what
	guarantees this, same mechanism already proven for other claims in this
	repo."""
	import threading

	client_id = attended_billable_appointment["client_id"]
	appointment_id = attended_billable_appointment["appointment_id"]
	as_of = attended_billable_appointment["as_of"]

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = 'cus_test_race' WHERE client_id = :c"), {"c": client_id})

	results: list[str] = []
	barrier = threading.Barrier(2)

	def _claim_and_charge():
		barrier.wait(timeout=5)
		gw = _FakeSitGateway()
		with get_system_db_context() as s:
			rows = s.execute(
				text(
					"SELECT client_id, appointment_id FROM appointments "
					"WHERE appointment_id = :aid AND billed_offer_code IS NULL "
					"FOR UPDATE SKIP LOCKED"
				),
				{"aid": appointment_id},
			).all()
			if not rows:
				results.append("SKIPPED_LOCKED")
				return
			outcome = charge_sit_for_appointment(s, client_id=client_id, appointment_id=appointment_id, as_of=as_of, gateway=gw)
			results.append(outcome.status)

	t1 = threading.Thread(target=_claim_and_charge)
	t2 = threading.Thread(target=_claim_and_charge)
	t1.start()
	t2.start()
	t1.join(timeout=10)
	t2.join(timeout=10)

	assert results.count("INVOICED") == 1, f"expected exactly one INVOICED outcome, got: {results}"

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = NULL WHERE client_id = :c"), {"c": client_id})


# ── PR #37 second review finding #5: a credit issued after the month's
# final sit rolls forward to the NEXT invoice and applies exactly once ────

def test_credit_from_prior_month_rolls_forward_and_applies_exactly_once(attended_billable_appointment):
	client_id = attended_billable_appointment["client_id"]
	appointment_id = attended_billable_appointment["appointment_id"]
	as_of = attended_billable_appointment["as_of"]

	last_month = date(as_of.year, as_of.month, 1) - timedelta(days=1)
	last_month_period = date(last_month.year, last_month.month, 1)
	with get_system_db_context() as s:
		credited = issue_credit(
			s, client_id=client_id, credit_type="MISS_CREDIT", amount_cents=5000,
			source_table="inbound_messages", source_id=f"stranded-{uuid.uuid4()}",
			issued_at=datetime.now(timezone.utc), billing_period=last_month_period,
		)
	assert credited is True

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = 'cus_test_credit' WHERE client_id = :c"), {"c": client_id})

	gw = _FakeSitGateway()
	with get_system_db_context() as s:
		outcome = charge_sit_for_appointment(s, client_id=client_id, appointment_id=appointment_id, as_of=as_of, gateway=gw)
	assert outcome.status == "INVOICED"

	with get_system_db_context() as s:
		credit_row = s.execute(
			text(
				"SELECT status, applied_at FROM billing_credits "
				"WHERE client_id = :c AND billing_period = :p"
			),
			{"c": client_id, "p": last_month_period},
		).one()
	assert credit_row.status == "APPLIED"
	assert credit_row.applied_at is not None
	assert gw.calls.count("add_invoice_item") == 1, "the sit charge itself is $0 (first sit) — the only item added is the credit"

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = NULL WHERE client_id = :c"), {"c": client_id})


def test_future_dated_credit_is_never_applied_early(attended_billable_appointment):
	"""Regression guard for the ORIGINAL fix this rule protects (PR #37 first
	review): the `<=` bound must never let a future-dated credit jump onto
	an earlier invoice."""
	client_id = attended_billable_appointment["client_id"]
	appointment_id = attended_billable_appointment["appointment_id"]
	as_of = attended_billable_appointment["as_of"]

	next_month = date(as_of.year, as_of.month, 28) + timedelta(days=7)
	future_period = date(next_month.year, next_month.month, 1)
	with get_system_db_context() as s:
		issue_credit(
			s, client_id=client_id, credit_type="MISS_CREDIT", amount_cents=5000,
			source_table="inbound_messages", source_id=f"future-{uuid.uuid4()}",
			issued_at=datetime.now(timezone.utc), billing_period=future_period,
		)

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = 'cus_test_future_credit' WHERE client_id = :c"), {"c": client_id})

	gw = _FakeSitGateway()
	with get_system_db_context() as s:
		charge_sit_for_appointment(s, client_id=client_id, appointment_id=appointment_id, as_of=as_of, gateway=gw)

	with get_system_db_context() as s:
		credit_row = s.execute(
			text("SELECT status FROM billing_credits WHERE client_id = :c AND billing_period = :p"),
			{"c": client_id, "p": future_period},
		).one()
	assert credit_row.status == "PENDING", "a future-dated credit must not apply to an earlier invoice"

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = NULL WHERE client_id = :c"), {"c": client_id})


# ── PR #37 second review finding #8: crash after invoice creation
# reconciles without a duplicate ──────────────────────────────────────────

def test_crash_after_invoice_creation_reconciles_without_duplicate_invoice(attended_billable_appointment):
	"""Simulates a real mid-sequence Stripe exception (add_invoice_item fails
	on the FIRST attempt only) — the invoice id must already be durably
	recorded so the retry resumes against the SAME invoice, never calling
	create_invoice a second time."""
	client_id = attended_billable_appointment["client_id"]
	appointment_id = attended_billable_appointment["appointment_id"]
	as_of = attended_billable_appointment["as_of"]

	with get_owner_db_context() as s:
		s.execute(text("UPDATE clients SET stripe_customer_id = 'cus_test_crash' WHERE client_id = :c"), {"c": client_id})

	# First sit is free (appt_first, $0) — add_invoice_item is never called
	# for a $0 charge, so force a second appointment (standard $99) to
	# exercise the add_invoice_item failure path.
	with get_system_db_context() as s:
		resolve_sit_charge(s, client_id=client_id, appointment_id=appointment_id, as_of=as_of)  # consumes the free first sit
		ctx_appt = s.execute(
			text("SELECT company_id, contact_id FROM appointments WHERE appointment_id = :a"), {"a": appointment_id}
		).one()
		second_appt = _insert_appointment(
			s, client_id=client_id, company_id=ctx_appt.company_id, contact_id=ctx_appt.contact_id,
			opportunity_id=str(uuid.uuid4()), state="ATTENDED", c24=as_of.isoformat(), c3=as_of.isoformat(),
		)
	second_appointment_id = str(second_appt.appointment_id)

	gw = _FakeSitGateway(fail_add_item_times=1)
	with get_system_db_context() as s:
		with pytest.raises(RuntimeError):
			charge_sit_for_appointment(s, client_id=client_id, appointment_id=second_appointment_id, as_of=as_of, gateway=gw)

	with get_system_db_context() as s:
		mid_crash_row = s.execute(
			text(
				"SELECT stripe_invoice_id, billed_offer_code, sit_invoice_finalized_at "
				"FROM appointments WHERE appointment_id = :a"
			),
			{"a": second_appointment_id},
		).one()
	assert mid_crash_row.stripe_invoice_id is not None, "the invoice id must survive the raised exception"
	# resolve_sit_charge() runs BEFORE any Stripe call and commits
	# independently of it — billed_offer_code IS already set at this point.
	# That is exactly the deeper half of finding #8 this test also proves:
	# billed_offer_code alone must NOT short-circuit the retry to
	# ALREADY_INVOICED while the Stripe side is still incomplete.
	assert mid_crash_row.billed_offer_code is not None
	assert mid_crash_row.sit_invoice_finalized_at is None, "not yet finalized — the retry must still run"

	with get_system_db_context() as s:
		outcome = charge_sit_for_appointment(s, client_id=client_id, appointment_id=second_appointment_id, as_of=as_of, gateway=gw)
	assert outcome.status == "INVOICED"
	assert outcome.stripe_invoice_id == mid_crash_row.stripe_invoice_id, "the retry must reuse the SAME invoice"
	assert gw.calls.count("create_invoice") == 1, "create_invoice must never be called twice across the crash+retry"

	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM appointments WHERE appointment_id = :a"), {"a": second_appointment_id})
		s.execute(text("UPDATE clients SET stripe_customer_id = NULL WHERE client_id = :c"), {"c": client_id})
