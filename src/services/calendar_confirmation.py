"""Atomic claim-and-send for booking confirmation email (Subtask 3.2.1).
Shared by the webhook route's immediate best-effort attempt (the
common-case fast path for the 60s promise — see
src/api/booking_webhook_router.py) and
src/tasks/booking_confirmation_sender.py's scheduled retry sweep, so
they're the identical code path, not two implementations that could
drift apart.

Atomic claiming alone does not guarantee exactly-once delivery — see
module-level notes on each function for why a recheck-before-send and an
UNCERTAIN outcome (distinct from a definite FAILED) both matter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.email_dispatch import send_booking_confirmation_email

_CLAIM_LEASE_MINUTES = 10
_MAX_ATTEMPTS_BEFORE_PERMANENT_FAILURE = 5


class UncertainDeliveryError(Exception):
	"""Raised by an EmailProvider implementation when a send's outcome
	genuinely can't be determined — e.g. a connection reset mid-DATA
	command, after the SMTP transaction had already started. Only
	email_dispatch.py's providers should raise this."""


def claim_confirmations(session: Session, *, booking_ids: Optional[List[int]] = None, limit: int = 20) -> List[int]:
	"""Atomically claims rows eligible to send/retry, including
	claim-expiry recovery: a row stuck in SENDING past the lease window
	means the worker that claimed it died mid-send (crash, OOM, deploy)
	before recording an outcome — without that branch such a row would
	never be retried or reconciled."""
	scope_clause = "AND booking_id = ANY(:booking_ids)" if booking_ids is not None else ""
	rows = session.execute(
		text(
			f"""
			UPDATE bookings SET confirmation_status = 'SENDING', confirmation_attempts = confirmation_attempts + 1,
								 confirmation_claimed_at = NOW()
			WHERE booking_id IN (
				SELECT booking_id FROM bookings
				WHERE (
					confirmation_status = 'PENDING'
					OR (confirmation_status = 'FAILED' AND confirmation_next_retry_at <= NOW())
					OR (confirmation_status = 'SENDING'
						AND confirmation_claimed_at < NOW() - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
				)
				{scope_clause}
				ORDER BY booking_id FOR UPDATE SKIP LOCKED LIMIT :limit
			)
			RETURNING booking_id
			"""
		),
		{"booking_ids": booking_ids, "limit": limit},
	).fetchall()
	return [r.booking_id for r in rows]


def _load_booking_for_send(session: Session, booking_id: int):
	return session.execute(
		text(
			"SELECT b.booking_id, b.client_id, b.event_status, b.confirmation_status, b.confirmation_attempts, "
			"b.scheduled_at, b.client_rep_email, oc.email AS owner_email, oc.full_name AS owner_name "
			"FROM bookings b LEFT JOIN owner_contacts oc ON b.owner_contact_id = oc.owner_contact_id "
			"WHERE b.booking_id = :id"
		),
		{"id": booking_id},
	).one()


def _mark(session: Session, booking_id: int, status: str, *, error: Optional[str] = None, next_retry_at=None) -> None:
	session.execute(
		text(
			"UPDATE bookings SET confirmation_status = :status, confirmation_last_error = :error, "
			"confirmation_next_retry_at = :next_retry_at, updated_at = NOW() WHERE booking_id = :id"
		),
		{"status": status, "error": error, "next_retry_at": next_retry_at, "id": booking_id},
	)


def send_confirmation_for_booking(session: Session, booking_id: int) -> None:
	"""Called only on a booking already claimed (confirmation_status =
	'SENDING') by claim_confirmations(). Rechecks cancellation immediately
	before calling the provider — not just at claim time — since a
	genuinely concurrent cancellation notification could land in between;
	if so, the send is aborted and the cancellation path's own
	'CANCELLED' status is left untouched, never overwritten back to
	'SENT'."""
	booking = _load_booking_for_send(session, booking_id)
	if booking.event_status == "CANCELLED" or booking.confirmation_status == "CANCELLED":
		return
	if booking.owner_email is None:
		_mark(session, booking_id, "FAILED_PERMANENT", error="no owner_contact email on file")
		return

	try:
		send_booking_confirmation_email(
			session,
			client_id=booking.client_id,
			booking_id=booking_id,
			owner_email=booking.owner_email,
			owner_name=booking.owner_name,
			client_reply_to=booking.client_rep_email or booking.owner_email,
			scheduled_at=booking.scheduled_at,
		)
	except UncertainDeliveryError as exc:
		# Distinct from a definite failure — a socket timeout/connection
		# reset after the SMTP transaction started means the real outcome
		# can't be known. Blindly retrying risks a duplicate real email
		# reaching the owner, which is worse than a missed one, so this is
		# surfaced for manual review rather than auto-retried.
		_mark(session, booking_id, "UNCERTAIN", error=str(exc))
		return
	except Exception as exc:  # noqa: BLE001 - any other provider failure is a definite, retryable failure
		if booking.confirmation_attempts >= _MAX_ATTEMPTS_BEFORE_PERMANENT_FAILURE:
			_mark(session, booking_id, "FAILED_PERMANENT", error=str(exc))
		else:
			backoff_minutes = 2 ** booking.confirmation_attempts
			_mark(
				session, booking_id, "FAILED", error=str(exc),
				next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes),
			)
		return

	_mark(session, booking_id, "SENT")


def attempt_immediate_confirmations(session: Session, booking_ids: List[int]) -> None:
	"""The common-case fast path: called right after sync_connection_locked
	inserts new PENDING confirmations, in the same background task, before
	the scheduled worker would otherwise pick them up. Uses the identical
	claim-and-send code path as booking_confirmation_sender.py, just
	scoped to these specific ids."""
	claimed = claim_confirmations(session, booking_ids=booking_ids, limit=len(booking_ids) or 1)
	for booking_id in claimed:
		send_confirmation_for_booking(session, booking_id)
