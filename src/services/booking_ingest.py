"""Application layer of Subtask 3.2.1 — Inbound Booking Engine.

Booking flow, corrected against Blackink_Source_of_Truth.md and three
rounds of engineering review (see the plan file for the full history):

- Google Calendar + Microsoft Graph only — no Calendly, no Blackink-owned
  calendar (client comment W1-8). The client's own representative/slot is
  still tracked, sourced from the calendar event's own organizer/attendee
  data, not a new user-account model.
- No SMS anywhere in this module (client comment W1-4, "we send no SMS
  this year") — this file never imports src.services.sms_dispatch.
- Owner identity is owner_contacts, not contacts (which is capped at two
  PM-firm staff roles per prospected company) and not owner_entities
  (unpopulated dedup scaffolding with no contact fields). An unmatched
  booking is queued in `bookings` with status='PENDING_RECONCILIATION'
  rather than a guessed contact/company being created — this queuing
  behavior is an engineering decision made this session, not a client
  requirement; no source document addresses the no-match case.
- Only calendar events carrying a private extended property
  (BOOKING_TAG_KEY/BOOKING_TAG_VALUE below) are ever turned into
  `bookings` rows — an ordinary client meeting on the same connected
  calendar must never become a booking or fire a confirmation. That tag
  is a contract this module hands to whichever (separate, not built
  here) subtask builds the owner-facing booking widget that actually
  creates these calendar events.
- Acknowledge-fast-process-async: this module's sync_connection() is
  never called synchronously from a webhook request handler — see
  src/api/booking_webhook_router.py, which only validates the provider
  handshake and enqueues into calendar_sync_queue, and
  src/tasks/calendar_sync_worker.py, which (along with a FastAPI
  BackgroundTask for the common-case fast path) is what actually calls
  this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

BOOKING_TAG_KEY = "blackink_booking"
BOOKING_TAG_VALUE = "1"

# Provider-supplied event.status values that mean "this slot no longer holds".
_CANCELLED_STATUSES = {"cancelled", "CANCELLED"}


class SyncTokenInvalidError(Exception):
	"""Raised by a CalendarProviderClient when the provider reports the
	stored sync_token/delta link is no longer valid (Google 410 Gone /
	Graph's equivalent resync-required response) — triggers a full resync,
	handled identically to any other sync via the same upsert-and-classify
	path (see module docstring: bookings already durably holds every
	previously-seen event, so a resync's "already seen" rows are a no-op)."""


@dataclass
class NormalizedEvent:
	"""Provider-agnostic shape a CalendarProviderClient must produce.
	Google and Microsoft Graph parsing are genuinely separate functions in
	src/services/calendar_providers.py — this dataclass is the only place
	their output has to agree."""

	external_event_id: str
	tagged: bool
	event_status: str  # 'CONFIRMED' | 'CANCELLED'
	scheduled_at: Optional[datetime]
	created_at: Optional[datetime]
	client_rep_name: Optional[str]
	client_rep_email: Optional[str]
	owner_name: Optional[str]
	owner_email: Optional[str]
	owner_phone: Optional[str]
	raw_payload: dict


@dataclass
class FetchResult:
	events: list
	sync_token: str


class CalendarProviderClient(Protocol):
	"""Implemented per-provider in src/services/calendar_providers.py.
	Deliberately not one shared code path pretending Google and Graph
	payloads are equivalent — see that module's docstring."""

	def fetch_baseline(self, connection) -> FetchResult: ...

	def fetch_incremental(self, connection) -> FetchResult: ...


@dataclass
class SyncOutcome:
	baseline: bool = False
	new_bookings: int = 0
	cancelled: int = 0
	rescheduled: int = 0
	unchanged: int = 0
	queued_confirmations: list = field(default_factory=list)  # booking_ids


def _record_event(session: Session, client_id: str, event_type: str, booking_id: int, payload: dict) -> None:
	session.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
			"VALUES (:client_id, :event_type, 'booking', :entity_id, CAST(:payload AS JSONB))"
		),
		{
			"client_id": client_id,
			"event_type": event_type,
			"entity_id": str(booking_id),
			"payload": json.dumps(payload),
		},
	)


def _find_owner_contact(session: Session, client_id: str, email: Optional[str]) -> Optional[int]:
	if not email:
		return None
	row = session.execute(
		text("SELECT owner_contact_id FROM owner_contacts WHERE client_id = :client_id AND email = :email LIMIT 1"),
		{"client_id": client_id, "email": email},
	).first()
	return row.owner_contact_id if row else None


def _upsert_booking(
	session: Session,
	*,
	client_id: str,
	provider: str,
	connection_id: int,
	event: NormalizedEvent,
	initial_confirmation_status: str,
) -> dict:
	"""The single atomic statement carrying all idempotency/race-safety for
	this module — see the plan doc for why a separate SELECT-then-INSERT
	was flagged as unsafe. Must run inside the caller's advisory-locked
	transaction (sync_connection() below)."""
	row = session.execute(
		text(
			"""
			WITH old AS (
				SELECT scheduled_at, event_status FROM bookings
				WHERE client_id = :client_id AND provider = :provider
				  AND calendar_connection_id = :connection_id AND external_event_id = :external_event_id
			),
			upsert AS (
				INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id,
										event_status, scheduled_at, client_rep_name, client_rep_email,
										raw_payload, confirmation_status)
				VALUES (:client_id, :provider, :connection_id, :external_event_id,
						:event_status, :scheduled_at, :client_rep_name, :client_rep_email,
						CAST(:raw_payload AS JSONB), :initial_confirmation_status)
				ON CONFLICT (client_id, provider, calendar_connection_id, external_event_id)
				DO UPDATE SET event_status = EXCLUDED.event_status, scheduled_at = EXCLUDED.scheduled_at,
							  client_rep_name = EXCLUDED.client_rep_name, client_rep_email = EXCLUDED.client_rep_email,
							  raw_payload = EXCLUDED.raw_payload, updated_at = NOW(),
							  confirmation_status = CASE WHEN EXCLUDED.event_status = 'CANCELLED'
														  AND bookings.confirmation_status IN ('PENDING', 'FAILED')
													  THEN 'CANCELLED' ELSE bookings.confirmation_status END
				RETURNING booking_id, (xmax = 0) AS was_inserted, event_status, scheduled_at
			)
			SELECT upsert.booking_id, upsert.was_inserted, upsert.event_status, upsert.scheduled_at,
				   old.scheduled_at AS old_scheduled_at, old.event_status AS old_event_status
			FROM upsert LEFT JOIN old ON true
			"""
		),
		{
			"client_id": client_id,
			"provider": provider,
			"connection_id": connection_id,
			"external_event_id": event.external_event_id,
			"event_status": event.event_status,
			"scheduled_at": event.scheduled_at,
			"client_rep_name": event.client_rep_name,
			"client_rep_email": event.client_rep_email,
			"raw_payload": json.dumps(event.raw_payload),
			"initial_confirmation_status": initial_confirmation_status,
		},
	).one()
	return dict(row._mapping)


def _cancel_by_identity(
	session: Session, *, client_id: str, provider: str, connection_id: int, external_event_id: str
) -> Optional[int]:
	"""Deletion/cancellation payloads sometimes omit the booking tag
	entirely (a minimal {id, status: 'cancelled'} notification) — applying
	the tag filter here (as an earlier draft did) would silently drop real
	cancellations of tagged bookings. Looks the event up by the same
	tenant-scoped identity as the unique constraint, never external_event_id
	alone, and is a no-op if this was never a Blackink booking."""
	row = session.execute(
		text(
			"""
			UPDATE bookings
			SET event_status = 'CANCELLED', updated_at = NOW(),
				confirmation_status = CASE WHEN confirmation_status IN ('PENDING', 'FAILED')
											THEN 'CANCELLED' ELSE confirmation_status END
			WHERE client_id = :client_id AND provider = :provider
			  AND calendar_connection_id = :connection_id AND external_event_id = :external_event_id
			  AND event_status <> 'CANCELLED'
			RETURNING booking_id
			"""
		),
		{
			"client_id": client_id,
			"provider": provider,
			"connection_id": connection_id,
			"external_event_id": external_event_id,
		},
	).first()
	return row.booking_id if row else None


def _process_event(
	session: Session,
	connection,
	connection_id: int,
	event: NormalizedEvent,
	outcome: SyncOutcome,
	*,
	connect_boundary: Optional[datetime],
) -> None:
	"""One event through the classify-upsert-dispatch pipeline, mutating
	`outcome` in place. Factored out of sync_connection()'s loop so
	process_ghl_event() (GoHighLevel's one-shot push model — no
	baseline/incremental fetch, one webhook call per booking) shares the
	exact same logic rather than a second copy that could drift."""
	if event.event_status in _CANCELLED_STATUSES or event.event_status == "CANCELLED":
		booking_id = _cancel_by_identity(
			session,
			client_id=connection.client_id,
			provider=connection.provider,
			connection_id=connection_id,
			external_event_id=event.external_event_id,
		)
		if booking_id:
			outcome.cancelled += 1
			_record_event(session, connection.client_id, "booking_cancelled", booking_id, {
				"external_event_id": event.external_event_id,
			})
		return

	if not event.tagged:
		return  # ordinary client meeting — never becomes a booking row

	is_baseline_suppressed = (
		outcome.baseline
		and connect_boundary is not None
		and event.created_at is not None
		and event.created_at < connect_boundary
	) or (outcome.baseline and connect_boundary is None)

	result = _upsert_booking(
		session,
		client_id=connection.client_id,
		provider=connection.provider,
		connection_id=connection_id,
		event=event,
		# Every insert starts NOT_REQUIRED regardless of baseline status;
		# eligible genuinely-new bookings are stepped to PENDING below,
		# after the owner-match lookup, in a separate UPDATE.
		initial_confirmation_status="NOT_REQUIRED",
	)

	if result["was_inserted"]:
		owner_contact_id = _find_owner_contact(session, connection.client_id, event.owner_email)
		match_status = "MATCHED" if owner_contact_id else "PENDING_RECONCILIATION"
		if owner_contact_id:
			session.execute(
				text(
					"UPDATE bookings SET owner_contact_id = :ocid, status = 'MATCHED', updated_at = NOW() "
					"WHERE booking_id = :bid"
				),
				{"ocid": owner_contact_id, "bid": result["booking_id"]},
			)

		eligible_for_confirmation = not is_baseline_suppressed
		if eligible_for_confirmation:
			session.execute(
				text("UPDATE bookings SET confirmation_status = 'PENDING' WHERE booking_id = :bid"),
				{"bid": result["booking_id"]},
			)
			outcome.queued_confirmations.append(result["booking_id"])

		_record_event(session, connection.client_id, "meeting_booked", result["booking_id"], {
			"external_event_id": event.external_event_id,
			"match_status": match_status,
			"baseline": is_baseline_suppressed,
		})
		outcome.new_bookings += 1
	else:
		if result["old_scheduled_at"] is not None and result["scheduled_at"] != result["old_scheduled_at"]:
			_record_event(session, connection.client_id, "booking_rescheduled", result["booking_id"], {
				"external_event_id": event.external_event_id,
				"old_scheduled_at": result["old_scheduled_at"].isoformat() if result["old_scheduled_at"] else None,
				"new_scheduled_at": result["scheduled_at"].isoformat() if result["scheduled_at"] else None,
			})
			outcome.rescheduled += 1
		else:
			outcome.unchanged += 1


def process_ghl_event(session: Session, connection_id: int, event: NormalizedEvent) -> SyncOutcome:
	"""GoHighLevel's webhook model is fundamentally different from Google/
	Microsoft: GHL has no OAuth app, no incremental-sync/pagination
	concept, and no separate fetch step — the full booking payload arrives
	directly in the webhook POST body (see src/services/ghl_webhook.py).
	This is the GHL entrypoint parallel to sync_connection_locked(),
	processing exactly one event through the identical pipeline
	(_process_event) rather than a parallel implementation. Must be called
	inside the same per-connection advisory lock as the Google/Microsoft
	path — see sync_connection_locked().
	"""
	connection = session.execute(
		text("SELECT * FROM calendar_connections WHERE connection_id = :id"),
		{"id": connection_id},
	).one()
	outcome = SyncOutcome(baseline=False)  # GHL has no baseline-sync concept — every event is "new"
	_process_event(session, connection, connection_id, event, outcome, connect_boundary=None)
	return outcome


def process_ghl_event_locked(session: Session, connection_id: int, event: NormalizedEvent) -> SyncOutcome:
	session.execute(
		text("SELECT pg_advisory_xact_lock(hashtext('calendar_sync:' || CAST(:cid AS TEXT)))"),
		{"cid": connection_id},
	)
	return process_ghl_event(session, connection_id, event)


def sync_connection(
	session: Session,
	connection_id: int,
	client: CalendarProviderClient,
	*,
	connect_boundary: Optional[datetime] = None,
) -> SyncOutcome:
	"""Runs the baseline-or-incremental sync for one connection. Must be
	called inside a transaction holding
	pg_advisory_xact_lock(hashtext('calendar_sync:' || connection_id)) —
	callers (calendar_oauth.py's connect callback, calendar_sync_worker.py)
	are responsible for acquiring that lock; this function assumes it.

	connect_boundary is only passed by the OAuth callback's baseline call
	(connect_initiated_at, captured before the subscription was
	registered) — see module docstring on the initialization-boundary fix:
	a baseline-fetch event created on/after that boundary is treated as a
	genuinely new booking, not suppressed, because it could have been
	created in the race window between subscribing and fetching.
	"""
	# Full row, not a narrow column list — CalendarProviderClient
	# implementations (src/services/calendar_providers.py) need the
	# encrypted token columns and subscription metadata this function
	# itself doesn't touch.
	connection = session.execute(
		text("SELECT * FROM calendar_connections WHERE connection_id = :id"),
		{"id": connection_id},
	).one()

	outcome = SyncOutcome(baseline=not connection.initial_sync_done)

	if not connection.initial_sync_done:
		fetch = client.fetch_baseline(connection)
	else:
		try:
			fetch = client.fetch_incremental(connection)
		except SyncTokenInvalidError:
			fetch = client.fetch_baseline(connection)
			# A forced resync reuses the baseline fetch call, but is NOT
			# the connection's initial baseline — every already-seen row
			# will report was_inserted=False via the upsert below, so no
			# separate suppression is needed here (see plan doc).
			outcome.baseline = False

	for event in fetch.events:
		_process_event(session, connection, connection_id, event, outcome, connect_boundary=connect_boundary)

	session.execute(
		text(
			"UPDATE calendar_connections SET sync_token = :token, initial_sync_done = TRUE, updated_at = NOW() "
			"WHERE connection_id = :id"
		),
		{"token": fetch.sync_token, "id": connection_id},
	)
	return outcome


def sync_connection_locked(
	session: Session,
	connection_id: int,
	client: CalendarProviderClient,
	*,
	connect_boundary: Optional[datetime] = None,
) -> SyncOutcome:
	"""Acquires the per-connection advisory lock and runs sync_connection().
	Every caller (the background task, calendar_sync_worker.py, and the
	OAuth callback's one-time baseline call) goes through this, not
	sync_connection() directly — a lock held only by convention in
	multiple call sites is a lock that will eventually get skipped
	somewhere. Safe to call redundantly: sync is idempotent, and a second
	caller simply waits for the first to release before finding nothing
	new to do."""
	session.execute(
		text("SELECT pg_advisory_xact_lock(hashtext('calendar_sync:' || CAST(:cid AS TEXT)))"),
		{"cid": connection_id},
	)
	return sync_connection(session, connection_id, client, connect_boundary=connect_boundary)


def link_booking_to_owner(session: Session, booking_id: int, owner_contact_id: int) -> None:
	"""Reconciliation action for a PENDING_RECONCILIATION booking. Must not
	rely on the FK alone — a foreign key on bookings.owner_contact_id only
	proves the referenced row exists, not that it belongs to the same
	client. Explicitly asserts tenant match before writing."""
	booking = session.execute(
		text("SELECT client_id FROM bookings WHERE booking_id = :id"), {"id": booking_id}
	).one()
	owner = session.execute(
		text("SELECT client_id FROM owner_contacts WHERE owner_contact_id = :id"), {"id": owner_contact_id}
	).one()
	if booking.client_id != owner.client_id:
		raise ValueError(
			f"Refusing to link booking {booking_id} (client {booking.client_id}) to "
			f"owner_contact {owner_contact_id} (client {owner.client_id}) - cross-tenant link rejected"
		)
	session.execute(
		text(
			"UPDATE bookings SET owner_contact_id = :ocid, status = 'MATCHED', updated_at = NOW() "
			"WHERE booking_id = :bid"
		),
		{"ocid": owner_contact_id, "bid": booking_id},
	)
	_record_event(session, booking.client_id, "booking_reconciled", booking_id, {"owner_contact_id": owner_contact_id})
