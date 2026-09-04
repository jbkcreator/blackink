"""Live-DB tests for Subtask 3.2.3's "Mark No-Show" prompt dispatch
(src/services/no_show_prompts.py, src/tasks/no_show_prompt_sender.py).

Same rationale as tests/test_show_rate_reminders.py for needing real
Postgres: the SKIP LOCKED atomic claim and the claim_time-parameterized
comparison can't be faithfully emulated by a FakeSession.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.services.no_show_prompts import claim_prompts, generate_no_show_token, verify_no_show_token

_BLACKINK_INTERNAL_SALES = "BLACKINK_INTERNAL_SALES"


def test_token_round_trips():
	token = generate_no_show_token(12345)
	assert verify_no_show_token(12345, token) is True
	assert verify_no_show_token(12345, "wrong-token") is False
	assert verify_no_show_token(99999, token) is False


@pytest.fixture
def sales_demo_booking():
	"""One INTERNAL_SALES_DEMO connection + one matched, CONFIRMED booking,
	built directly via SQL for precise control over scheduled_at/target_contact_id."""
	with get_owner_db_context() as session:
		session.execute(text(
			"DELETE FROM no_show_prompt_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"DELETE FROM no_show_recovery_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"DELETE FROM booking_reminder_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"UPDATE contacts SET outbound_pause_source_booking_id = NULL WHERE outbound_pause_source_booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
		session.execute(text("DELETE FROM calendar_connections WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM contacts WHERE email = 'no-show-target@example.com'"))
		session.execute(text("DELETE FROM companies WHERE company_id = 'test-no-show-co'"))
		company = session.execute(
			text(
				"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
				"VALUES ('test-no-show-co', 'Test No-Show PM Co', 'noshow-test.example.com', "
				"(SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') RETURNING company_id"
			)
		).one()
		contact = session.execute(
			text(
				"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
				"VALUES (:cid, 'OWNER_BROKER_MD', 'Sam', 'Target', 'no-show-target@example.com', '+14075551234') "
				"RETURNING contact_id"
			),
			{"cid": company.company_id},
		).one()

	with get_system_db_context() as session:
		conn = session.execute(
			text(
				"INSERT INTO calendar_connections "
				"(client_id, provider, connection_scope, external_calendar_id, subscription_id, "
				" verification_secret, initial_sync_done) "
				"VALUES (:cid, 'GOOGLE', 'INTERNAL_SALES_DEMO', 'rep-primary', 'sub-no-show', 'secret', TRUE) "
				"RETURNING connection_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES},
		).one()
		scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=2)
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, scheduled_at, raw_payload, target_company_id, target_contact_id, status) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-no-show', 'CONFIRMED', :sched, '{}', "
				":company_id, :contact_id, 'MATCHED') RETURNING booking_id"
			),
			{
				"cid": _BLACKINK_INTERNAL_SALES, "conn": conn.connection_id, "sched": scheduled_at,
				"company_id": company.company_id, "contact_id": contact.contact_id,
			},
		).one()
		job = session.execute(
			text(
				"INSERT INTO no_show_prompt_jobs (client_id, booking_id, scheduled_for, status) "
				"VALUES (:cid, :bid, :sched, 'PENDING') RETURNING prompt_job_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES, "bid": booking.booking_id, "sched": scheduled_at},
		).one()

	yield {"booking_id": booking.booking_id, "prompt_job_id": job.prompt_job_id, "scheduled_at": scheduled_at}


def test_claim_prompts_claims_due_pending_job(sales_demo_booking):
	with get_system_db_context() as session:
		claimed = claim_prompts(session, claim_time=datetime.now(timezone.utc))
	assert sales_demo_booking["prompt_job_id"] in claimed


def test_claim_prompts_does_not_reclaim_within_lease(sales_demo_booking):
	with get_system_db_context() as session:
		claim_prompts(session, claim_time=datetime.now(timezone.utc))
		claimed_again = claim_prompts(session, claim_time=datetime.now(timezone.utc))
	assert sales_demo_booking["prompt_job_id"] not in claimed_again
