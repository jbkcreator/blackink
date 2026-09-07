"""Atomic claim-and-send for the no-show recovery email (Subtask 3.2.3)
— mirrors show_rate_reminders.py's SKIP LOCKED pattern. The Slack click
handler only ever INSERTs a PENDING no_show_recovery_jobs row (no SMTP
call inline); this module is what actually sends, on a short-interval
sweep, with a full recheck immediately before sending — the booking or
the contact's own state may have changed in the time between the click
and this claim.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.services.booking_link import resolve_booking_link
from src.services.calendar_confirmation import UncertainDeliveryError
from src.services.email_dispatch import send_no_show_recovery_email

_CLAIM_LEASE_MINUTES = 10
_MAX_ATTEMPTS_BEFORE_FAILED = 5


def claim_recovery_jobs(session: Session, *, claim_time: datetime, limit: int = 20) -> List[int]:
	rows = session.execute(
		text(
			f"""
			UPDATE no_show_recovery_jobs SET status = 'SENDING', attempts = attempts + 1, claimed_at = :claim_time
			WHERE recovery_job_id IN (
				SELECT recovery_job_id FROM no_show_recovery_jobs
				WHERE (status = 'PENDING' AND (next_retry_at IS NULL OR next_retry_at <= :claim_time))
				   OR (status = 'FAILED' AND next_retry_at <= :claim_time)
				   OR (status = 'SENDING' AND claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
				ORDER BY recovery_job_id FOR UPDATE SKIP LOCKED LIMIT :limit
			)
			RETURNING recovery_job_id
			"""
		),
		{"claim_time": claim_time, "limit": limit},
	).fetchall()
	return [r.recovery_job_id for r in rows]


def _mark(session: Session, recovery_job_id: int, status: str, *, error: Optional[str] = None, next_retry_at=None, sent: bool = False) -> None:
	session.execute(
		text(
			"UPDATE no_show_recovery_jobs SET status = :status, last_error = :error, "
			"next_retry_at = :next_retry_at, sent_at = CASE WHEN :sent THEN NOW() ELSE sent_at END, "
			"updated_at = NOW() WHERE recovery_job_id = :id"
		),
		{"status": status, "error": error, "next_retry_at": next_retry_at, "sent": sent, "id": recovery_job_id},
	)


def send_recovery_email(session: Session, recovery_job_id: int) -> None:
	"""Called only on a job already claimed (status='SENDING'). Rechecks
	before sending — per the correction this implements, a click and a
	send are not the same moment, and the contact/booking may have moved
	on in between:
	  - contact already resumed (rebooked) -> CANCELLED, no send.
	  - booking no longer eligible (cancelled) -> SKIPPED, no send.
	  - email disabled, or no booking-link redirect available -> BLOCKED.
	"""
	job = session.execute(
		text(
			"SELECT rj.recovery_job_id, rj.booking_id, rj.contact_id, rj.attempts, "
			"b.client_id, b.event_status, "
			"c.email AS target_email, c.first_name AS target_first_name, "
			"c.outbound_paused_at, c.outbound_pause_source_booking_id "
			"FROM no_show_recovery_jobs rj "
			"JOIN bookings b ON rj.booking_id = b.booking_id "
			"LEFT JOIN contacts c ON rj.contact_id = c.contact_id "
			"WHERE rj.recovery_job_id = :id"
		),
		{"id": recovery_job_id},
	).one()

	if job.event_status == "CANCELLED":
		_mark(session, recovery_job_id, "SKIPPED", error="booking was cancelled before send")
		return

	# The pause was set with outbound_pause_source_booking_id = this
	# booking_id at trigger time. If it's now NULL (resume_contact_if_rebooked
	# cleared it) or points at a DIFFERENT booking, the contact has already
	# rebooked — sending a "let's reschedule" email to someone who already
	# rescheduled would be a wrong, unnecessary send.
	if job.outbound_paused_at is None or job.outbound_pause_source_booking_id != job.booking_id:
		_mark(session, recovery_job_id, "CANCELLED", error="contact already rebooked")
		return

	if job.target_email is None:
		_mark(session, recovery_job_id, "SKIPPED", error="no target contact email on file")
		return

	if not get_settings().email_sending_enabled:
		_mark(session, recovery_job_id, "BLOCKED", error="EMAIL_SENDING_DISABLED")
		return

	link = resolve_booking_link(session, name=job.target_first_name, email=job.target_email)
	if link is None:
		_mark(session, recovery_job_id, "BLOCKED", error="NO_BOOKING_LINK_AVAILABLE")
		return

	try:
		message_id = send_no_show_recovery_email(
			session,
			client_id=job.client_id,
			target_email=job.target_email,
			target_name=job.target_first_name,
			booking_url=link.url,
		)
	except UncertainDeliveryError as exc:
		_mark(session, recovery_job_id, "UNCERTAIN", error=str(exc))
		return
	except Exception as exc:  # noqa: BLE001 - any other provider failure is a definite, retryable failure
		if job.attempts >= _MAX_ATTEMPTS_BEFORE_FAILED:
			_mark(session, recovery_job_id, "FAILED", error=str(exc))
		else:
			backoff_minutes = 2 ** job.attempts
			_mark(
				session, recovery_job_id, "FAILED", error=str(exc),
				next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes),
			)
		return

	if message_id is None:
		# send_no_show_recovery_email's own email_sending_enabled/mailbox
		# checks already returned None — BLOCKED, not silently dropped.
		_mark(session, recovery_job_id, "BLOCKED", error="EMAIL_SENDING_DISABLED")
		return

	_mark(session, recovery_job_id, "SENT", sent=True)
	session.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
			"VALUES (:client_id, 'no_show_recovery_sent', 'booking', :entity_id, '{}'::jsonb)"
		),
		{"client_id": job.client_id, "entity_id": str(job.booking_id)},
	)
