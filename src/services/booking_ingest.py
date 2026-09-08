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

Extended for Subtask 3.2.2 (Show-Rate Reminder Cascade):
`calendar_connections.connection_scope` distinguishes the original
owner-books-client's-calendar flow above (CLIENT_OWNER_BOOKING) from a
prospective PM firm booking a sales-demo call with Blackink's own sales
team (INTERNAL_SALES_DEMO) — the DoD's "prospect's Owner Visibility
Score PDF" only makes sense for the latter (the OVS scores `companies`,
i.e. PM firms, not residential owners). An INTERNAL_SALES_DEMO booking
matches its target against `contacts`/`companies` by work email instead
of `owner_contacts`, and `schedule_show_rate_reminders()` is called on
every insert/reschedule/cancellation of such a booking — never for
CLIENT_OWNER_BOOKING bookings, since the DoD's OVS-PDF/benchmark
content is specific to the sales-demo flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


def _find_sales_demo_target(session: Session, email: Optional[str]) -> Optional[dict]:
	"""INTERNAL_SALES_DEMO counterpart to _find_owner_contact(): the
	work-email captured on the booking form is matched against
	contacts/companies (global, deduplicated — not client-scoped, since
	the target is a prospective PM firm, not this reserved internal
	client's own tenant data), never owner_contacts.

	Goes through the resolve_sales_demo_target() SECURITY DEFINER function
	(apply_bookings.py), not a direct SELECT — contacts/companies are
	RLS-scoped via companies.owning_client_id, which is NULL for every
	PROSPECTING/ENGAGED company (not yet allocated to a client), so a
	session scoped to client_id='BLACKINK_INTERNAL_SALES' can never see
	them directly (NULL never matches an RLS equality filter). Same
	pattern already used for resolve_calendar_connection()."""
	if not email:
		return None
	row = session.execute(
		text("SELECT * FROM resolve_sales_demo_target(:email)"),
		{"email": email},
	).first()
	return {"contact_id": row.contact_id, "company_id": row.company_id} if row else None


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


_REMINDER_STEPS = {
	"24h_email": timedelta(hours=24),
	"30min_email": timedelta(minutes=30),
}


def schedule_show_rate_reminders(
	session: Session,
	*,
	client_id: str,
	booking_id: int,
	old_event_status: Optional[str],
	old_scheduled_at: Optional[datetime],
	new_event_status: str,
	new_scheduled_at: Optional[datetime],
	as_of: Optional[datetime] = None,
) -> None:
	"""Called on every upsert of an INTERNAL_SALES_DEMO-scope booking —
	new inserts, reschedules, AND cancellations alike (not gated on
	was_inserted, which would silently miss reschedules/cancellations
	entirely). Fire time is an absolute TIMESTAMPTZ offset from
	new_scheduled_at, so no timezone math is needed here — see
	src/services/show_rate_reminders.py for where timezone resolution
	matters (email content only)."""
	as_of = as_of or datetime.now(timezone.utc)

	if new_event_status == "CANCELLED":
		session.execute(
			text(
				"UPDATE booking_reminder_jobs SET status = 'CANCELLED', updated_at = NOW() "
				"WHERE booking_id = :bid AND status IN ('PENDING', 'FAILED', 'BLOCKED', 'SENDING')"
			),
			{"bid": booking_id},
		)
		return

	if new_event_status != "CONFIRMED" or new_scheduled_at is None:
		return

	is_new = old_event_status is None
	is_reschedule = not is_new and old_scheduled_at != new_scheduled_at

	if not is_new and not is_reschedule:
		return  # unchanged CONFIRMED booking (e.g. a routine incremental re-sync) — no-op

	for step, offset in _REMINDER_STEPS.items():
		scheduled_for = new_scheduled_at - offset
		status = "SKIPPED" if scheduled_for <= as_of else "PENDING"
		last_error = f"scheduled_for ({scheduled_for.isoformat()}) already past at {'creation' if is_new else 'reschedule'} time" if status == "SKIPPED" else None
		if is_new:
			session.execute(
				text(
					"INSERT INTO booking_reminder_jobs (client_id, booking_id, reminder_step, scheduled_for, status, last_error) "
					"VALUES (:client_id, :booking_id, :step, :scheduled_for, :status, :last_error) "
					"ON CONFLICT (booking_id, reminder_step) DO NOTHING"
				),
				{
					"client_id": client_id, "booking_id": booking_id, "step": step,
					"scheduled_for": scheduled_for, "status": status, "last_error": last_error,
				},
			)
		else:
			# Reschedule: only touch rows still PENDING/FAILED/BLOCKED — a
			# SENT reminder is never re-sent or un-sent.
			session.execute(
				text(
					"UPDATE booking_reminder_jobs SET scheduled_for = :scheduled_for, status = :status, "
					"last_error = :last_error, updated_at = NOW() "
					"WHERE booking_id = :booking_id AND reminder_step = :step "
					"AND status IN ('PENDING', 'FAILED', 'BLOCKED')"
				),
				{
					"scheduled_for": scheduled_for, "status": status, "last_error": last_error,
					"booking_id": booking_id, "step": step,
				},
			)


def schedule_no_show_prompt(
	session: Session,
	*,
	client_id: str,
	booking_id: int,
	old_event_status: Optional[str],
	old_scheduled_at: Optional[datetime],
	new_event_status: str,
	new_scheduled_at: Optional[datetime],
	as_of: Optional[datetime] = None,
) -> None:
	"""Subtask 3.2.3 — No-Show Handler. Sibling to
	schedule_show_rate_reminders() — same insert/reschedule/cancel call
	sites and lifecycle rules, but for the single 'Mark No-Show' Slack
	prompt job rather than an email reminder. Reads bookings.target_contact_id
	itself (rather than taking it as a parameter) so every call site is
	uniform whether the target was just resolved (new insert) or was
	resolved earlier (reschedule/cancel of an already-matched booking).

	Never schedules an actionable PENDING prompt for a booking whose
	target_contact_id is unresolved (still PENDING_RECONCILIATION) — a
	rep clicking "Mark No-Show" on an inferred/unknown contact would pause
	the wrong person. Such a booking's job stays BLOCKED until
	reconciliation resolves target_contact_id (no automatic re-check exists
	yet for this specific case — a future reconciliation-completion hook
	would need to re-run this function)."""
	as_of = as_of or datetime.now(timezone.utc)

	if new_event_status == "CANCELLED":
		session.execute(
			text(
				"UPDATE no_show_prompt_jobs SET status = 'CANCELLED', updated_at = NOW() "
				"WHERE booking_id = :bid AND status IN ('PENDING', 'BLOCKED', 'SENDING')"
			),
			{"bid": booking_id},
		)
		return

	if new_event_status != "CONFIRMED" or new_scheduled_at is None:
		return

	is_new = old_event_status is None
	is_reschedule = not is_new and old_scheduled_at != new_scheduled_at
	if not is_new and not is_reschedule:
		return  # unchanged CONFIRMED booking — no-op

	target_contact_id = session.execute(
		text("SELECT target_contact_id FROM bookings WHERE booking_id = :bid"),
		{"bid": booking_id},
	).scalar()

	if target_contact_id is None:
		status, last_error = "BLOCKED", "target_contact_id unresolved (booking is PENDING_RECONCILIATION)"
	elif new_scheduled_at <= as_of:
		status = "SKIPPED"
		last_error = f"scheduled_for ({new_scheduled_at.isoformat()}) already past at {'creation' if is_new else 'reschedule'} time"
	else:
		status, last_error = "PENDING", None

	if is_new:
		session.execute(
			text(
				"INSERT INTO no_show_prompt_jobs (client_id, booking_id, scheduled_for, status, last_error) "
				"VALUES (:client_id, :booking_id, :scheduled_for, :status, :last_error) "
				"ON CONFLICT (booking_id) DO NOTHING"
			),
			{
				"client_id": client_id, "booking_id": booking_id, "scheduled_for": new_scheduled_at,
				"status": status, "last_error": last_error,
			},
		)
	else:
		# Reschedule: only touch rows still PENDING/BLOCKED — a SENT/CANCELLED
		# job is never re-posted.
		session.execute(
			text(
				"UPDATE no_show_prompt_jobs SET scheduled_for = :scheduled_for, status = :status, "
				"last_error = :last_error, updated_at = NOW() "
				"WHERE booking_id = :booking_id AND status IN ('PENDING', 'BLOCKED')"
			),
			{
				"scheduled_for": new_scheduled_at, "status": status, "last_error": last_error,
				"booking_id": booking_id,
			},
		)


def schedule_meeting_outcome_prompt(
	session: Session,
	*,
	client_id: str,
	booking_id: int,
	old_event_status: Optional[str],
	old_scheduled_at: Optional[datetime],
	new_event_status: str,
	new_scheduled_at: Optional[datetime],
	as_of: Optional[datetime] = None,
) -> None:
	"""Addendum to Subtask 3.2.1 — the "Log Outcome" trigger card. Third
	sibling to schedule_show_rate_reminders()/schedule_no_show_prompt():
	identical signature, identical call sites (insert, reschedule AND
	cancellation), identical lifecycle rules.

	scheduled_for is the meeting's own start time, never a guessed end time
	— the card exists to give the closer a button to press whenever their
	meeting actually wraps; the click, not any timer, is what signals "the
	meeting is done."

	Unlike the no-show prompt, an unresolved target_contact_id here is not
	the only blocking input: the card must @mention (and later authorize) the
	assigned closer, which needs calendar_connections.rep_slack_user_id.
	Both blocking conditions are re-checked at post time by
	src/services/meeting_outcome_prompts.py, which is also where the
	self-heal that lifts them lives — this function only needs to get the
	row scheduled, so it does the cheap target check it already has a query
	for and leaves the connection lookup to the poster."""
	as_of = as_of or datetime.now(timezone.utc)

	if new_event_status == "CANCELLED":
		# No outcome prompt for a meeting that never happened — and a card
		# ALREADY posted (SENT) must be invalidated too, not left live for its
		# 24h TTL: otherwise a booking cancelled after its card was posted
		# could still be clicked and submitted as attended/no-show. The job
		# goes CANCELLED here; the click and submit handlers additionally
		# re-check the booking's live status, so the already-posted Slack card
		# becomes inert the moment it's used.
		session.execute(
			text(
				"UPDATE meeting_outcome_prompt_jobs SET status = 'CANCELLED', updated_at = NOW() "
				"WHERE booking_id = :bid AND status IN ('PENDING', 'BLOCKED', 'SENDING', 'SENT')"
			),
			{"bid": booking_id},
		)
		return

	if new_event_status != "CONFIRMED" or new_scheduled_at is None:
		return

	is_new = old_event_status is None
	is_reschedule = not is_new and old_scheduled_at != new_scheduled_at
	if not is_new and not is_reschedule:
		return  # unchanged CONFIRMED booking — no-op

	target_contact_id = session.execute(
		text("SELECT target_contact_id FROM bookings WHERE booking_id = :bid"),
		{"bid": booking_id},
	).scalar()

	if target_contact_id is None:
		status, last_error = "BLOCKED", "UNRESOLVED_TARGET"
	elif new_scheduled_at <= as_of:
		status = "SKIPPED"
		last_error = f"scheduled_for ({new_scheduled_at.isoformat()}) already past at {'creation' if is_new else 'reschedule'} time"
	else:
		status, last_error = "PENDING", None

	if is_new:
		session.execute(
			text(
				"INSERT INTO meeting_outcome_prompt_jobs (client_id, booking_id, scheduled_for, status, last_error) "
				"VALUES (:client_id, :booking_id, :scheduled_for, :status, :last_error) "
				"ON CONFLICT (booking_id) DO NOTHING"
			),
			{
				"client_id": client_id, "booking_id": booking_id, "scheduled_for": new_scheduled_at,
				"status": status, "last_error": last_error,
			},
		)
	else:
		# Reschedule: only touch rows still PENDING/BLOCKED — a SENT card is
		# never re-posted, and a CANCELLED/EXPIRED one is never revived.
		session.execute(
			text(
				"UPDATE meeting_outcome_prompt_jobs SET scheduled_for = :scheduled_for, status = :status, "
				"last_error = :last_error, updated_at = NOW() "
				"WHERE booking_id = :booking_id AND status IN ('PENDING', 'BLOCKED')"
			),
			{
				"scheduled_for": new_scheduled_at, "status": status, "last_error": last_error,
				"booking_id": booking_id,
			},
		)


def _resume_if_rebooked(session: Session, *, contact_id: int, new_booking_id: int) -> None:
	"""Subtask 3.2.3. Clears a contact's outbound pause ONLY when the
	contact has actually rebooked under a genuinely new, distinct
	booking_id — never on a routine re-sync/re-delivery of the SAME
	booking whose no-show caused the pause (that would silently defeat
	the pause the moment the provider redelivers an unchanged webhook
	event). Only ever called from the was_inserted branch below, so
	new_booking_id is always a booking_id that did not exist before this
	call — still compared explicitly against
	outbound_pause_source_booking_id rather than assumed, since that's the
	actual invariant that matters here, not merely "this code path only
	runs on insert."

	Goes through resume_contact_if_rebooked() (apply_bookings.py), not a
	direct UPDATE — contacts is RLS-scoped via companies.owning_client_id,
	which is NULL for every unallocated prospect, so a session scoped to
	client_id='BLACKINK_INTERNAL_SALES' can never write these rows
	directly (same reason _find_sales_demo_target() above goes through
	resolve_sales_demo_target() rather than a direct SELECT)."""
	session.execute(
		text("SELECT resume_contact_if_rebooked(:cid, :new_booking_id)"),
		{"cid": contact_id, "new_booking_id": new_booking_id},
	)


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
			if connection.connection_scope == "INTERNAL_SALES_DEMO":
				schedule_show_rate_reminders(
					session, client_id=connection.client_id, booking_id=booking_id,
					old_event_status="CONFIRMED", old_scheduled_at=None,
					new_event_status="CANCELLED", new_scheduled_at=None,
				)
				schedule_no_show_prompt(
					session, client_id=connection.client_id, booking_id=booking_id,
					old_event_status="CONFIRMED", old_scheduled_at=None,
					new_event_status="CANCELLED", new_scheduled_at=None,
				)
				schedule_meeting_outcome_prompt(
					session, client_id=connection.client_id, booking_id=booking_id,
					old_event_status="CONFIRMED", old_scheduled_at=None,
					new_event_status="CANCELLED", new_scheduled_at=None,
				)
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

	is_sales_demo = connection.connection_scope == "INTERNAL_SALES_DEMO"

	if result["was_inserted"]:
		if is_sales_demo:
			target = _find_sales_demo_target(session, event.owner_email)
			match_status = "MATCHED" if target else "PENDING_RECONCILIATION"
			if target:
				session.execute(
					text(
						"UPDATE bookings SET target_company_id = :cid, target_contact_id = :ctid, "
						"status = 'MATCHED', updated_at = NOW() WHERE booking_id = :bid"
					),
					{"cid": target["company_id"], "ctid": target["contact_id"], "bid": result["booking_id"]},
				)
				# A genuinely new booking_id (this is the was_inserted branch)
				# for a matched contact means the contact has rebooked — clear
				# any standing no-show pause, but only if it wasn't already
				# sourced from this exact booking (can't be, since this
				# booking_id did not exist until this INSERT).
				_resume_if_rebooked(
					session, contact_id=target["contact_id"], new_booking_id=result["booking_id"]
				)
		else:
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
			# Subtask 3.1.2 — a win-back owner booking a meeting must stop
			# their touch sequence, regardless of match_status: winback
			# owners are deliberately never linked to owner_contacts (3.1.1's
			# own decision), so this can never resolve to MATCHED above —
			# this is a separate, additive lookup keyed purely on email, not
			# a change to the owner-matching logic itself.
			if event.owner_email:
				from src.services.winback_sequencer import stop_active_winback_runs
				stop_active_winback_runs(session, connection.client_id, event.owner_email, "MEETING_BOOKED")

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

		if is_sales_demo and eligible_for_confirmation:
			schedule_show_rate_reminders(
				session, client_id=connection.client_id, booking_id=result["booking_id"],
				old_event_status=None, old_scheduled_at=None,
				new_event_status="CONFIRMED", new_scheduled_at=result["scheduled_at"],
			)
			schedule_no_show_prompt(
				session, client_id=connection.client_id, booking_id=result["booking_id"],
				old_event_status=None, old_scheduled_at=None,
				new_event_status="CONFIRMED", new_scheduled_at=result["scheduled_at"],
			)
			schedule_meeting_outcome_prompt(
				session, client_id=connection.client_id, booking_id=result["booking_id"],
				old_event_status=None, old_scheduled_at=None,
				new_event_status="CONFIRMED", new_scheduled_at=result["scheduled_at"],
			)
	else:
		if result["old_scheduled_at"] is not None and result["scheduled_at"] != result["old_scheduled_at"]:
			_record_event(session, connection.client_id, "booking_rescheduled", result["booking_id"], {
				"external_event_id": event.external_event_id,
				"old_scheduled_at": result["old_scheduled_at"].isoformat() if result["old_scheduled_at"] else None,
				"new_scheduled_at": result["scheduled_at"].isoformat() if result["scheduled_at"] else None,
			})
			outcome.rescheduled += 1
			if is_sales_demo:
				schedule_show_rate_reminders(
					session, client_id=connection.client_id, booking_id=result["booking_id"],
					old_event_status="CONFIRMED", old_scheduled_at=result["old_scheduled_at"],
					new_event_status="CONFIRMED", new_scheduled_at=result["scheduled_at"],
				)
				schedule_no_show_prompt(
					session, client_id=connection.client_id, booking_id=result["booking_id"],
					old_event_status="CONFIRMED", old_scheduled_at=result["old_scheduled_at"],
					new_event_status="CONFIRMED", new_scheduled_at=result["scheduled_at"],
				)
				schedule_meeting_outcome_prompt(
					session, client_id=connection.client_id, booking_id=result["booking_id"],
					old_event_status="CONFIRMED", old_scheduled_at=result["old_scheduled_at"],
					new_event_status="CONFIRMED", new_scheduled_at=result["scheduled_at"],
				)
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
	# A booking that reached the confirmation queue before this reconciliation
	# always failed immediately with this exact error (send_confirmation_for_booking
	# marks FAILED_PERMANENT the instant owner_email is NULL) — that status is
	# never reclaimed by claim_confirmations(), so without this reset the
	# confirmation email is lost permanently. Scoped to this one error string:
	# NOT_REQUIRED bookings (baseline-suppressed, pre-dating the connection)
	# must stay untouched forever regardless of reconciliation, and a
	# FAILED_PERMANENT from a real provider error (max attempts exhausted)
	# must not be silently resurrected just because an owner got matched.
	session.execute(
		text(
			"UPDATE bookings SET confirmation_status = 'PENDING', confirmation_attempts = 0, "
			"confirmation_last_error = NULL, confirmation_next_retry_at = NULL, updated_at = NOW() "
			"WHERE booking_id = :bid AND confirmation_status = 'FAILED_PERMANENT' "
			"AND confirmation_last_error = 'no owner_contact email on file'"
		),
		{"bid": booking_id},
	)
	_record_event(session, booking.client_id, "booking_reconciled", booking_id, {"owner_contact_id": owner_contact_id})
