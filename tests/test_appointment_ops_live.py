"""Live-DB schema verification for the appointment-operations migration
(Subtask 1.1.1). Requires a real Postgres with migrations applied — the same
class as tests/test_tenant_isolation.py.

Covers the DoD lines a FakeSession cannot: the is_billable generated-column
truth table, the CHECK constraints rejecting out-of-domain values, the two
required indices existing, and opportunity_id being shareable across multiple
appointment rows (the non-unique idx_opportunity_dedupe, reschedule chains).
The pure state-machine rules are in tests/test_appointment_state.py.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError

from src.core.database import get_owner_db_context, get_system_db_context
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401


def _insert_appointment(session, *, client_id, company_id, contact_id, opportunity_id,
						 state="BOOKED", c24=None, c3=None):
	return session.execute(
		text(
			"INSERT INTO appointments "
			"(client_id, company_id, opportunity_id, contact_id, state, scheduled_for, "
			" confirmed_24h_timestamp, confirmed_3h_timestamp, owner_brief_url) "
			"VALUES (:client_id, :company_id, :opp, :contact_id, CAST(:state AS appointment_state_enum), "
			" NOW(), :c24, :c3, 'https://brief.example/x') "
			"RETURNING appointment_id, is_billable"
		),
		{"client_id": client_id, "company_id": company_id, "opp": opportunity_id,
		 "contact_id": contact_id, "state": state, "c24": c24, "c3": c3},
	).one()


@pytest.fixture
def appt(canary_tenants):
	"""Real client/company/contact from the canary fixture, with appointment-row
	cleanup that runs BEFORE the canary teardown (which would otherwise fail on
	the client_id FK if appointment rows survived)."""
	ctx = canary_tenants[CANARY_A]
	yield {"client_id": CANARY_A, **ctx}
	# DELETE is REVOKEd from both runtime roles on these tables (a row is
	# status-transitioned, never removed at runtime), so cleanup runs as the
	# table owner — same posture as test_self_serve_audit_worker_live.py.
	with get_owner_db_context() as s:
		s.execute(text("DELETE FROM appointment_disputes WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM appointment_dispositions WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM confirmation_logs WHERE client_id = :c"), {"c": CANARY_A})
		s.execute(text("DELETE FROM appointments WHERE client_id = :c"), {"c": CANARY_A})


def test_is_billable_truth_table(appt):
	"""TRUE only when state=ATTENDED AND both confirmation timestamps are set —
	all three gate combinations, per the DoD."""
	now = "2026-01-01T00:00:00+00:00"
	with get_system_db_context() as s:
		# ATTENDED + both confirmations -> billable.
		r_yes = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
									 contact_id=appt["contact_id"], opportunity_id=str(uuid.uuid4()),
									 state="ATTENDED", c24=now, c3=now)
		# ATTENDED but missing the 3h confirmation -> not billable.
		r_missing = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
										contact_id=appt["contact_id"], opportunity_id=str(uuid.uuid4()),
										state="ATTENDED", c24=now, c3=None)
		# Both confirmations but not yet ATTENDED -> not billable.
		r_notyet = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
									   contact_id=appt["contact_id"], opportunity_id=str(uuid.uuid4()),
									   state="CONFIRMED_3H", c24=now, c3=now)
	assert r_yes.is_billable is True
	assert r_missing.is_billable is False
	assert r_notyet.is_billable is False


def test_is_billable_flips_false_when_state_leaves_attended(appt):
	"""Source D caveat 457: is_billable drops to false on ATTENDED -> DISPOSITIONED.
	The generated column recomputes on UPDATE — proving it is not a one-way latch."""
	now = "2026-01-01T00:00:00+00:00"
	with get_system_db_context() as s:
		row = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
								  contact_id=appt["contact_id"], opportunity_id=str(uuid.uuid4()),
								  state="ATTENDED", c24=now, c3=now)
		assert row.is_billable is True
		after = s.execute(
			text("UPDATE appointments SET state='DISPOSITIONED' WHERE appointment_id=:id "
				 "RETURNING is_billable"),
			{"id": row.appointment_id},
		).scalar()
	assert after is False


def test_confirmation_logs_channel_check_rejects_bad_value(appt):
	with get_system_db_context() as s:
		row = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
								  contact_id=appt["contact_id"], opportunity_id=str(uuid.uuid4()))
		with pytest.raises((IntegrityError, DataError)):
			s.execute(
				text("INSERT INTO confirmation_logs "
					 "(client_id, appointment_id, channel, confirmation_tier, sent_at, delivery_status) "
					 "VALUES (:c, :a, 'PHONE', '24H', NOW(), 'sent')"),
				{"c": appt["client_id"], "a": row.appointment_id},
			)


def test_confirmation_logs_tier_check_rejects_bad_value(appt):
	with get_system_db_context() as s:
		row = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
								  contact_id=appt["contact_id"], opportunity_id=str(uuid.uuid4()))
		with pytest.raises((IntegrityError, DataError)):
			s.execute(
				text("INSERT INTO confirmation_logs "
					 "(client_id, appointment_id, channel, confirmation_tier, sent_at, delivery_status) "
					 "VALUES (:c, :a, 'SMS', '12H', NOW(), 'sent')"),
				{"c": appt["client_id"], "a": row.appointment_id},
			)


def test_required_indices_exist():
	with get_system_db_context() as s:
		names = {
			r[0]
			for r in s.execute(
				text("SELECT indexname FROM pg_indexes WHERE tablename = 'appointments'")
			).all()
		}
	assert "idx_appointments_billing_gate" in names
	assert "idx_opportunity_dedupe" in names


def test_opportunity_id_shared_across_reschedule_rows(appt):
	"""idx_opportunity_dedupe is non-unique on purpose: one opportunity spans
	multiple appointment rows across reschedules. Two rows with the same
	opportunity_id must both persist."""
	opp = str(uuid.uuid4())
	with get_system_db_context() as s:
		first = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
									contact_id=appt["contact_id"], opportunity_id=opp, state="RESCHEDULED")
		second = _insert_appointment(s, client_id=appt["client_id"], company_id=appt["company_id"],
									 contact_id=appt["contact_id"], opportunity_id=opp, state="REBOOKED")
		count = s.execute(
			text("SELECT COUNT(*) FROM appointments WHERE opportunity_id = :o"), {"o": opp}
		).scalar()
	assert first.appointment_id != second.appointment_id
	assert count == 2
