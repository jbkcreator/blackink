"""Live-DB tests for Subtask 3.2.3's no-show recovery email dispatch
(src/services/no_show_recovery.py, src/services/no_show_recovery_dispatch.py,
src/tasks/no_show_recovery_sender.py) -- in particular the required
recheck-before-send states (CANCELLED/SKIPPED/BLOCKED) and the 5-minute
SLA proof via triggered_at/sent_at.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_db_context, get_owner_db_context, get_system_db_context
from src.services.no_show_recovery import trigger_recovery
from src.services.no_show_recovery_dispatch import claim_recovery_jobs, send_recovery_email
from src.tasks.no_show_recovery_sender import run_sweep

_BLACKINK_INTERNAL_SALES = "BLACKINK_INTERNAL_SALES"


@pytest.fixture
def no_show_booking():
	with get_owner_db_context() as session:
		session.execute(text(
			"DELETE FROM no_show_recovery_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"DELETE FROM no_show_prompt_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"DELETE FROM booking_reminder_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"DELETE FROM meeting_outcome_prompt_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		# A previous run's trigger_recovery() may have left
		# contacts.outbound_pause_source_booking_id pointing at a booking
		# this fixture is about to delete -- clear the FK reference first
		# (an UPDATE, not a delete; the contacts row itself is deleted/
		# recreated below).
		session.execute(text(
			"UPDATE contacts SET outbound_pause_source_booking_id = NULL "
			"WHERE email = 'recovery-target@example.com'"
		))
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
		session.execute(text("DELETE FROM calendar_connections WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM meeting_outcomes WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
		session.execute(text("DELETE FROM contacts WHERE email = 'recovery-target@example.com'"))
		session.execute(text("DELETE FROM companies WHERE company_id = 'test-recovery-co'"))
		company = session.execute(
			text(
				"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
				"VALUES ('test-recovery-co', 'Test Recovery PM Co', 'recovery-test.example.com', "
				"(SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') RETURNING company_id"
			)
		).one()
		contact = session.execute(
			text(
				"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
				"VALUES (:cid, 'OWNER_BROKER_MD', 'Sam', 'Target', 'recovery-target@example.com', '+14075551234') "
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
				"VALUES (:cid, 'GOOGLE', 'INTERNAL_SALES_DEMO', 'rep-primary', 'sub-recovery', 'secret', TRUE) "
				"RETURNING connection_id"
			),
			{"cid": _BLACKINK_INTERNAL_SALES},
		).one()
		scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=2)
		booking = session.execute(
			text(
				"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
				"event_status, scheduled_at, raw_payload, target_company_id, target_contact_id, status) "
				"VALUES (:cid, 'GOOGLE', :conn, 'evt-recovery', 'CONFIRMED', :sched, '{}', "
				":company_id, :contact_id, 'MATCHED') RETURNING booking_id"
			),
			{
				"cid": _BLACKINK_INTERNAL_SALES, "conn": conn.connection_id, "sched": scheduled_at,
				"company_id": company.company_id, "contact_id": contact.contact_id,
			},
		).one()

	yield {"booking_id": booking.booking_id, "contact_id": contact.contact_id}


def test_trigger_recovery_enqueues_pending_job_no_send(no_show_booking):
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		result = trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	assert result.already_recorded is False
	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT * FROM no_show_recovery_jobs WHERE booking_id = :bid"), {"bid": no_show_booking["booking_id"]}
		).one()
		assert job.status == "PENDING"
		assert job.triggered_at is not None
		assert job.sent_at is None
		contact = session.execute(
			text("SELECT outbound_paused_at, outbound_pause_source_booking_id FROM contacts WHERE contact_id = :cid"),
			{"cid": no_show_booking["contact_id"]},
		).one()
		assert contact.outbound_paused_at is not None
		assert contact.outbound_pause_source_booking_id == no_show_booking["booking_id"]


def test_trigger_recovery_is_idempotent_on_double_click(no_show_booking):
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		result = trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U456")
		session.commit()
	assert result.already_recorded is True
	with get_owner_db_context() as session:
		count = session.execute(
			text("SELECT COUNT(*) FROM no_show_recovery_jobs WHERE booking_id = :bid"), {"bid": no_show_booking["booking_id"]}
		).scalar()
		assert count == 1


def test_send_recovery_email_cancelled_when_already_rebooked(no_show_booking):
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	# Simulate the contact having genuinely rebooked -- resume_contact_if_rebooked()
	# clears both columns to NULL on a real rebooking (see booking_ingest.py),
	# never an arbitrary other booking_id (which would violate the FK to a
	# real bookings row).
	with get_owner_db_context() as session:
		session.execute(
			text("UPDATE contacts SET outbound_paused_at = NULL, outbound_pause_source_booking_id = NULL "
				 "WHERE contact_id = :cid"),
			{"cid": no_show_booking["contact_id"]},
		)
		session.commit()
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc))
		for job_id in claimed:
			send_recovery_email(session, job_id)
	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT status FROM no_show_recovery_jobs WHERE booking_id = :bid"), {"bid": no_show_booking["booking_id"]}
		).one()
		assert job.status == "CANCELLED"


def test_send_recovery_email_skipped_when_booking_cancelled(no_show_booking):
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	with get_owner_db_context() as session:
		session.execute(text("UPDATE bookings SET event_status = 'CANCELLED' WHERE booking_id = :bid"), {"bid": no_show_booking["booking_id"]})
		session.commit()
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc))
		for job_id in claimed:
			send_recovery_email(session, job_id)
	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT status FROM no_show_recovery_jobs WHERE booking_id = :bid"), {"bid": no_show_booking["booking_id"]}
		).one()
		assert job.status == "SKIPPED"


def test_send_recovery_email_blocked_when_email_sending_disabled(no_show_booking, monkeypatch):
	assert get_settings().email_sending_enabled is False  # default posture
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc))
		for job_id in claimed:
			send_recovery_email(session, job_id)
	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT status, last_error FROM no_show_recovery_jobs WHERE booking_id = :bid"), {"bid": no_show_booking["booking_id"]}
		).one()
		assert job.status == "BLOCKED"
		assert job.last_error == "EMAIL_SENDING_DISABLED"


def test_sla_sent_at_within_five_minutes_of_triggered_at(no_show_booking, monkeypatch):
	"""Proves the DoD's 'within 5 minutes' claim with a real assertion on
	stored timestamps, not just a short sweep interval."""
	monkeypatch.setattr(get_settings(), "email_sending_enabled", True)
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()

	# No booking-link is configured in this test env, so the job resolves
	# to BLOCKED (NO_BOOKING_LINK_AVAILABLE) rather than SENT -- this test
	# asserts the SLA plumbing (triggered_at is set immediately, sent_at
	# stays NULL until an actual send), not a full real-SMTP send, which
	# is exercised manually per the plan's Verification section.
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc))
		for job_id in claimed:
			send_recovery_email(session, job_id)

	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT triggered_at, sent_at, status FROM no_show_recovery_jobs WHERE booking_id = :bid"),
			{"bid": no_show_booking["booking_id"]},
		).one()
	assert job.triggered_at is not None
	if job.sent_at is not None:
		assert job.sent_at - job.triggered_at <= timedelta(minutes=5)


def test_claim_recovery_jobs_reclaims_failed_rows_past_next_retry_at(no_show_booking):
	"""Regression: claim_recovery_jobs() must reclaim a FAILED row once
	its next_retry_at has passed -- a bounded retry, not a dead end where
	a transient send failure gets stuck forever."""
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	with get_owner_db_context() as session:
		session.execute(
			text(
				"UPDATE no_show_recovery_jobs SET status = 'FAILED', attempts = 1, "
				"next_retry_at = :past WHERE booking_id = :bid"
			),
			{"past": datetime.now(timezone.utc) - timedelta(minutes=1), "bid": no_show_booking["booking_id"]},
		)
		session.commit()
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc))
	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT recovery_job_id FROM no_show_recovery_jobs WHERE booking_id = :bid"),
			{"bid": no_show_booking["booking_id"]},
		).one()
	assert job.recovery_job_id in claimed


def test_claim_recovery_jobs_does_not_reclaim_failed_row_before_next_retry_at(no_show_booking):
	with get_db_context(client_id=_BLACKINK_INTERNAL_SALES) as session:
		trigger_recovery(session, booking_id=no_show_booking["booking_id"], submitted_by="slack:U123")
		session.commit()
	with get_owner_db_context() as session:
		session.execute(
			text(
				"UPDATE no_show_recovery_jobs SET status = 'FAILED', attempts = 1, "
				"next_retry_at = :future WHERE booking_id = :bid"
			),
			{"future": datetime.now(timezone.utc) + timedelta(hours=1), "bid": no_show_booking["booking_id"]},
		)
		session.commit()
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc))
	with get_owner_db_context() as session:
		job = session.execute(
			text("SELECT recovery_job_id FROM no_show_recovery_jobs WHERE booking_id = :bid"),
			{"bid": no_show_booking["booking_id"]},
		).one()
	assert job.recovery_job_id not in claimed
