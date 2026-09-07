"""Atomic claim-and-post for the post-booking "Log Outcome" trigger card
(Addendum to Subtask 3.2.1) — mirrors src/services/no_show_prompts.py's
SKIP LOCKED / explicit-claim_time pattern.

Two things distinguish this from the "Mark No-Show" prompt it sits beside:

  * The card is a real agent_work_orders row, so its SHA-256 binding over
    payload + recipient + config state comes from the existing
    src/services/slack/payload_hash.py — no second hash implementation, per
    the addendum's own "do not build a second implementation" line. The
    24-hour expiry is NOT in that hash (the preimage is FIXED; adding a
    timestamp window would change every digest and force a HASH_VERSION
    bump) — it's an explicit created_at + 24h check in the click/submit
    handlers, plus the EXPIRED transition in sweep_posted_cards() below.

  * There is no late-claim window. Unlike the no-show prompt (meaningful
    only within 10 minutes of the meeting's start), this card is meaningful
    for its whole 24-hour life — the closer clicks it whenever the meeting
    actually wraps. So a delayed sweep still posts, and it's the 24-hour
    expiry, not the claim time, that closes the card out.

Runs under the BYPASSRLS system session (see the sender task): contacts and
companies are RLS-scoped through companies.owning_client_id, which is NULL
for every unallocated prospect, so a session scoped to the internal-sales
client could not read the prospect name this card has to display.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services import work_orders as wo
from src.services.slack import payload_hash, post

logger = logging.getLogger(__name__)

_CLAIM_LEASE_MINUTES = 10

# The card's own life, and the one nudge inside it. Both anchored on
# posted_at (when the card actually reached Slack), never on the meeting
# time — a card posted late still gets its full window.
CARD_TTL = timedelta(hours=24)
UNCLICKED_REMINDER_AFTER = timedelta(hours=4)

_ACTION_CLASS = "MEETING_OUTCOME_PROMPT"
_BLOCKED_REASONS_SELF_HEALABLE = ("UNRESOLVED_TARGET", "MISSING_REP_SLACK_USER_ID")


def outcome_recorded_for_booking(session: Session, booking_id: int) -> bool:
	"""Any meeting_outcomes row for this booking, of any attendance_status.

	Deliberately broader than no_show_recovery.already_recorded(), which
	only looks for NO_SHOW: a booking already marked no-show from the
	"Mark No-Show" card in #blackink-command must not also get an outcome
	prompt in #blackink-setter, and an outcome logged from either surface
	closes the question for both.

	meeting_outcomes has no booking_id column, so the join is on the pair
	that identifies the meeting — the booking's own target_contact_id and
	scheduled_at — exactly as already_recorded() does."""
	row = session.execute(
		text(
			"SELECT 1 FROM meeting_outcomes mo "
			"JOIN bookings b ON b.target_contact_id = mo.contact_id "
			"WHERE b.booking_id = :bid AND mo.meeting_occurred_at = b.scheduled_at LIMIT 1"
		),
		{"bid": booking_id},
	).first()
	return row is not None


def self_heal_blocked(session: Session) -> int:
	"""Promotes BLOCKED rows back to PENDING once the input that blocked
	them actually appears — an unresolved target getting reconciled, or an
	operator finally filling in calendar_connections.rep_slack_user_id.

	Only the two reasons in _BLOCKED_REASONS_SELF_HEALABLE are ever
	promoted, and only while the booking is still CONFIRMED. Same posture
	as show_rate_reminders.py's MISSING_OVS_* self-heal: a row reappearing
	is evidence the underlying data arrived, not that some other, unrelated
	block was resolved.

	Returns the number of rows promoted."""
	result = session.execute(
		text(
			"UPDATE meeting_outcome_prompt_jobs j SET status = 'PENDING', last_error = NULL, "
			"updated_at = NOW() "
			"FROM bookings b JOIN calendar_connections cc ON cc.connection_id = b.calendar_connection_id "
			"WHERE j.booking_id = b.booking_id AND j.status = 'BLOCKED' "
			"AND j.last_error = ANY(:reasons) "
			"AND b.event_status = 'CONFIRMED' "
			"AND b.target_contact_id IS NOT NULL AND b.target_company_id IS NOT NULL "
			"AND cc.rep_slack_user_id IS NOT NULL"
		),
		{"reasons": list(_BLOCKED_REASONS_SELF_HEALABLE)},
	)
	return result.rowcount or 0


def claim_prompts(session: Session, *, claim_time: datetime, limit: int = 20) -> List[int]:
	"""PENDING rows whose scheduled_for has arrived AND whose retry backoff
	(next_retry_at) has elapsed, plus claim-expired SENDING rows (a worker
	that crashed mid-post). BLOCKED, SKIPPED, CANCELLED, EXPIRED and SENT are
	never reclaimed — a BLOCKED row only returns to PENDING via
	self_heal_blocked(). A transient Slack post failure re-queues the row as
	PENDING with a next_retry_at backoff (see post_prompt), so it is picked up
	again here rather than stranded — the reason next_retry_at is honored."""
	rows = session.execute(
		text(
			f"""
			UPDATE meeting_outcome_prompt_jobs SET status = 'SENDING', claimed_at = :claim_time
			WHERE prompt_job_id IN (
				SELECT prompt_job_id FROM meeting_outcome_prompt_jobs
				WHERE (status = 'PENDING' AND scheduled_for <= :claim_time
				       AND (next_retry_at IS NULL OR next_retry_at <= :claim_time))
				   OR (status = 'SENDING' AND claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
				ORDER BY prompt_job_id FOR UPDATE SKIP LOCKED LIMIT :limit
			)
			RETURNING prompt_job_id
			"""
		),
		{"claim_time": claim_time, "limit": limit},
	).fetchall()
	return [r.prompt_job_id for r in rows]


# A transient Slack failure is retried this many times (bounded exponential
# backoff) before the job is marked terminally FAILED. Comfortably covers a
# short Slack outage without retrying forever.
_MAX_POST_ATTEMPTS = 5


def _mark(session: Session, prompt_job_id: int, status: str, *, error: Optional[str] = None) -> None:
	session.execute(
		text(
			"UPDATE meeting_outcome_prompt_jobs SET status = :status, last_error = :error, "
			"updated_at = NOW() WHERE prompt_job_id = :id"
		),
		{"status": status, "error": error, "id": prompt_job_id},
	)


def _mark_post_failure(session: Session, prompt_job_id: int, attempts: int, error: str) -> None:
	"""A Slack post failed. If the bounded attempt budget is exhausted, mark
	terminally FAILED; otherwise re-queue as PENDING with an exponential
	backoff so a later sweep retries — a transient Slack outage must not
	permanently strand the closer's card."""
	next_attempts = attempts + 1
	if next_attempts >= _MAX_POST_ATTEMPTS:
		session.execute(
			text(
				"UPDATE meeting_outcome_prompt_jobs SET status = 'FAILED', attempts = :n, "
				"last_error = :err, updated_at = NOW() WHERE prompt_job_id = :id"
			),
			{"n": next_attempts, "err": error, "id": prompt_job_id},
		)
		return
	backoff_minutes = 2 ** next_attempts
	session.execute(
		text(
			"UPDATE meeting_outcome_prompt_jobs SET status = 'PENDING', attempts = :n, "
			"last_error = :err, next_retry_at = NOW() + (:mins || ' minutes')::interval, "
			"updated_at = NOW() WHERE prompt_job_id = :id"
		),
		{"n": next_attempts, "err": error, "mins": str(backoff_minutes), "id": prompt_job_id},
	)


def _prospect_name(first_name: Optional[str], last_name: Optional[str]) -> str:
	full = " ".join(part for part in (first_name, last_name) if part)
	return full or "(name unknown)"


def card_blocks(
	*,
	prospect_name: str,
	company_name: str,
	scheduled_at: datetime,
	rep_slack_user_id: str,
	button_value: str,
) -> list:
	"""The card's blocks. The meeting time is rendered with Slack's own
	<!date^…> token so every viewer sees it in their own timezone — this
	card lands in a shared channel and a raw UTC string would be read wrong
	by whoever is not in UTC. The fallback after the pipe is what shows in
	notifications and on clients that can't render the token."""
	ts = int(scheduled_at.timestamp())
	when = f"<!date^{ts}^{{date_short_pretty}} at {{time}}|{scheduled_at.isoformat()}>"
	return [
		{
			"type": "section",
			"text": {
				"type": "mrkdwn",
				"text": (
					f":memo: *Log the outcome of this demo*\n"
					f"*Prospect:* {prospect_name}\n"
					f"*Company:* {company_name}\n"
					f"*Scheduled:* {when}\n"
					f"*Closer:* <@{rep_slack_user_id}>"
				),
			},
		},
		{
			"type": "actions",
			"elements": [
				{
					"type": "button",
					"text": {"type": "plain_text", "text": "Log Outcome"},
					"style": "primary",
					"action_id": "log_meeting_outcome",
					"value": button_value,
				}
			],
		},
	]


async def post_prompt(session: Session, prompt_job_id: int, *, as_of: datetime) -> None:
	"""Called only on a job already claimed (status='SENDING') by
	claim_prompts(). Rechecks every blocking condition against live state
	immediately before posting — the booking, its reconciliation status and
	the rep mapping can all have moved since the row was scheduled."""
	job = session.execute(
		text(
			"SELECT j.prompt_job_id, j.booking_id, j.scheduled_for, j.attempts, b.client_id, b.event_status, "
			"b.scheduled_at, b.target_contact_id, b.target_company_id, b.client_rep_email, "
			"b.calendar_connection_id, cc.rep_slack_user_id, c.first_name, c.last_name, co.company_name "
			"FROM meeting_outcome_prompt_jobs j "
			"JOIN bookings b ON j.booking_id = b.booking_id "
			"JOIN calendar_connections cc ON cc.connection_id = b.calendar_connection_id "
			"LEFT JOIN contacts c ON c.contact_id = b.target_contact_id "
			"LEFT JOIN companies co ON co.company_id = b.target_company_id "
			"WHERE j.prompt_job_id = :id"
		),
		{"id": prompt_job_id},
	).one()

	if job.event_status == "CANCELLED":
		# No outcome prompt for a meeting that never happened.
		_mark(session, prompt_job_id, "CANCELLED", error="booking cancelled before the card was posted")
		return

	if job.target_contact_id is None or job.target_company_id is None:
		# meeting_outcomes.contact_id/company_id are NOT NULL FKs — a card
		# whose submission could not be written is worse than no card, and
		# an inferred contact must never be attributed an outcome.
		_mark(session, prompt_job_id, "BLOCKED", error="UNRESOLVED_TARGET")
		return

	# The assigned closer is derived from the booking's own rep-calendar-slot
	# email (the addendum's DoD sources the @mention from that webhook field),
	# resolved to a Slack id via users.lookupByEmail and CACHED onto
	# calendar_connections.rep_slack_user_id so the lookup runs at most once
	# per rep. A manually-provisioned column value is honored as an override
	# and skips the lookup entirely.
	rep_slack_user_id = job.rep_slack_user_id
	if not rep_slack_user_id and job.client_rep_email:
		rep_slack_user_id = await post.lookup_user_id_by_email(job.client_rep_email)
		if rep_slack_user_id:
			session.execute(
				text("UPDATE calendar_connections SET rep_slack_user_id = :rep, updated_at = NOW() "
				     "WHERE connection_id = :cid AND rep_slack_user_id IS NULL"),
				{"rep": rep_slack_user_id, "cid": job.calendar_connection_id},
			)

	if not rep_slack_user_id:
		# Neither an override column nor a resolvable rep-slot email — the card
		# can neither @mention the assigned closer nor authorize a click
		# against them. self_heal_blocked() promotes this row the moment an
		# operator provisions the column (the lookup-failure path is not
		# auto-retried, since a not-found email won't resolve on its own).
		_mark(session, prompt_job_id, "BLOCKED", error="MISSING_REP_SLACK_USER_ID")
		return

	if outcome_recorded_for_booking(session, job.booking_id):
		_mark(session, prompt_job_id, "SKIPPED", error="OUTCOME_ALREADY_RECORDED")
		return

	prospect_name = _prospect_name(job.first_name, job.last_name)
	company_name = job.company_name or "(company unknown)"
	meeting_occurred_at = job.scheduled_at.isoformat()

	# The card is a work order, so hash binding/verification is the existing
	# shared implementation. idempotency_key is per-booking, so a re-post
	# (claim-lease recovery after a crash between enqueue and the Slack call)
	# returns the SAME order rather than creating a second one.
	order = wo.enqueue(
		client_id=job.client_id,
		entity_type="booking",
		entity_id=str(job.booking_id),
		agent_id="booking_engine",
		action_class=_ACTION_CLASS,
		autonomy_band="BAND_2_ONE_TAP",
		risk_class="LOW",
		recipient=rep_slack_user_id,
		payload={
			"booking_id": job.booking_id,
			"contact_id": job.target_contact_id,
			"company_id": job.target_company_id,
			"meeting_occurred_at": meeting_occurred_at,
			"prospect_name": prospect_name,
			"company_name": company_name,
		},
		config_fingerprint={"card": _ACTION_CLASS, "channel_key": "setter", "ttl_hours": 24},
		idempotency_key=f"meeting_outcome_prompt|{job.booking_id}",
	)

	# contact_id as an int-parseable string and meeting_occurred_at as ISO
	# 8601 — the exact shapes open_meeting_outcome_modal() validates. The
	# hash is RECOMPUTED here, never read off the row (see
	# payload_hash.button_value's docstring for why).
	button_value = json.dumps(
		{
			"client_id": order.client_id,
			"action_id": order.action_id,
			"contact_id": str(job.target_contact_id),
			"meeting_occurred_at": meeting_occurred_at,
			"payload_hash": payload_hash.compute(order),
		},
		separators=(",", ":"),
	)

	posted = await post.post_action_card(
		channel_key="setter",
		text=f"Log the outcome of the demo with {prospect_name} ({company_name})",
		blocks=card_blocks(
			prospect_name=prospect_name,
			company_name=company_name,
			scheduled_at=job.scheduled_at,
			rep_slack_user_id=rep_slack_user_id,
			button_value=button_value,
		),
	)
	if posted is None:
		# Transient Slack failure (API/channel/config hiccup) — re-queue with a
		# bounded backoff rather than terminally FAILED on the first miss, so a
		# short outage at meeting time doesn't permanently strand the card.
		_mark_post_failure(session, prompt_job_id, job.attempts, "Slack post failed or unconfigured")
		return

	wo.set_slack_message(
		order.client_id, order.action_id,
		channel_id=posted["channel_id"], message_ts=posted["message_ts"],
	)
	session.execute(
		text(
			"UPDATE meeting_outcome_prompt_jobs SET status = 'SENT', last_error = NULL, "
			"work_order_action_id = :action_id, slack_channel_id = :cid, slack_message_ts = :ts, "
			"posted_at = :posted_at, updated_at = NOW() WHERE prompt_job_id = :id"
		),
		{
			"action_id": order.action_id, "cid": posted["channel_id"], "ts": posted["message_ts"],
			"posted_at": as_of, "id": prompt_job_id,
		},
	)


async def sweep_posted_cards(session: Session, *, as_of: datetime, limit: int = 100) -> dict:
	"""The missed-window fallback and the end of the card's life.

	For every SENT card:
	  * still unclicked 4 hours after posting -> exactly one reminder ping,
	    threaded under the card itself.
	  * 24 hours after posting -> EXPIRED. The card's hash-bound click is
	    already rejected past that point by the click handler's own TTL
	    check; this transition is what stops the sweep re-examining the row
	    and guarantees no further reminders, matching how every other
	    expired card in the system behaves.

	"Unclicked" is read off the work order (still QUEUED) and confirmed
	against meeting_outcomes — a click that opened the modal but was
	abandoned has logged nothing, so it still deserves the nudge."""
	rows = session.execute(
		text(
			"SELECT prompt_job_id, booking_id, client_id, work_order_action_id, "
			"slack_message_ts, posted_at, reminder_sent_at "
			"FROM meeting_outcome_prompt_jobs "
			"WHERE status = 'SENT' AND posted_at IS NOT NULL "
			"ORDER BY prompt_job_id LIMIT :limit"
		),
		{"limit": limit},
	).fetchall()

	pinged = expired = 0
	for row in rows:
		if as_of >= row.posted_at + CARD_TTL:
			_mark(session, row.prompt_job_id, "EXPIRED", error="24h card window closed with no outcome logged")
			expired += 1
			continue

		if row.reminder_sent_at is not None or as_of < row.posted_at + UNCLICKED_REMINDER_AFTER:
			continue

		if outcome_recorded_for_booking(session, row.booking_id):
			continue

		order = wo.get(row.client_id, str(row.work_order_action_id)) if row.work_order_action_id else None
		if order is None or order.status != "QUEUED":
			continue

		ts = await post.post_notice(
			channel_key="setter",
			text=(
				f":bell: Still waiting on the outcome for this demo — <@{order.recipient}>, "
				f"use the *Log Outcome* button above. The card expires 24 hours after posting."
			),
			thread_ts=row.slack_message_ts,
		)
		if ts is None:
			# Slack unconfigured or the post failed. Leave reminder_sent_at
			# NULL so the next sweep retries rather than silently swallowing
			# the only nudge this card ever gets.
			continue

		session.execute(
			text(
				"UPDATE meeting_outcome_prompt_jobs SET reminder_sent_at = :now, updated_at = NOW() "
				"WHERE prompt_job_id = :id"
			),
			{"now": as_of, "id": row.prompt_job_id},
		)
		pinged += 1

	return {"pinged": pinged, "expired": expired}
