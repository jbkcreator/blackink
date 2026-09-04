"""No-show recording + recovery-job enqueue (Subtask 3.2.3). Called
synchronously from src/services/slack/listeners.py's mark_no_show click
handler, inside ONE transaction, with NO external network call inside
it — the recovery email itself is sent later, out-of-band, by
src/tasks/no_show_recovery_sender.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.meeting_outcomes import MeetingOutcomeRecord, record_meeting_outcome

_INTERNAL_SALES_CLIENT_ID = "BLACKINK_INTERNAL_SALES"


@dataclass(frozen=True)
class NoShowResult:
	already_recorded: bool
	recovery_job_id: int = None


def already_recorded(session: Session, booking_id: int) -> bool:
	"""Idempotent double-click guard — checked by the Slack handler before
	calling trigger_recovery(), and re-checked here as a second line of
	defense against a race between two concurrent clicks."""
	row = session.execute(
		text(
			"SELECT 1 FROM meeting_outcomes mo "
			"JOIN bookings b ON b.target_contact_id = mo.contact_id "
			"WHERE b.booking_id = :bid AND mo.attendance_status = 'NO_SHOW' "
			"AND mo.meeting_occurred_at = b.scheduled_at LIMIT 1"
		),
		{"bid": booking_id},
	).first()
	return row is not None


def trigger_recovery(session: Session, *, booking_id: int, submitted_by: str) -> NoShowResult:
	"""One transaction, no external calls: record the NO_SHOW outcome,
	pause the contact (via the SECURITY DEFINER function — see
	apply_bookings.py's pause_contact_after_no_show()), enqueue exactly
	one recovery-email job, log the event. Caller (listeners.py) commits
	after this returns and only THEN responds to Slack / touches the card
	— nothing here blocks on SMTP or any other network I/O."""
	booking = session.execute(
		text(
			"SELECT booking_id, client_id, target_company_id, target_contact_id, scheduled_at "
			"FROM bookings WHERE booking_id = :bid"
		),
		{"bid": booking_id},
	).one()

	if already_recorded(session, booking_id):
		return NoShowResult(already_recorded=True)

	record_meeting_outcome(
		session,
		MeetingOutcomeRecord(
			client_id=_INTERNAL_SALES_CLIENT_ID,
			contact_id=booking.target_contact_id,
			company_id=booking.target_company_id,
			meeting_occurred_at=booking.scheduled_at,
			attendance_status="NO_SHOW",
			submitted_by=submitted_by,
		),
	)

	session.execute(
		text("SELECT pause_contact_after_no_show(:cid, :bid)"),
		{"cid": booking.target_contact_id, "bid": booking_id},
	)

	recovery_job_id = session.execute(
		text(
			"INSERT INTO no_show_recovery_jobs (client_id, booking_id, contact_id, status) "
			"VALUES (:client_id, :booking_id, :contact_id, 'PENDING') "
			"ON CONFLICT (booking_id) DO NOTHING RETURNING recovery_job_id"
		),
		{"client_id": booking.client_id, "booking_id": booking_id, "contact_id": booking.target_contact_id},
	).scalar()

	session.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
			"VALUES (:client_id, 'no_show_triggered', 'booking', :entity_id, :actor, :payload)"
		),
		{
			"client_id": booking.client_id, "entity_id": str(booking_id), "actor": submitted_by,
			"payload": json.dumps({"contact_id": booking.target_contact_id}),
		},
	)

	return NoShowResult(already_recorded=False, recovery_job_id=recovery_job_id)
