"""Atomic claim-and-post for the "Mark No-Show" Slack prompt (Subtask
3.2.3) — mirrors src/services/show_rate_reminders.py's SKIP LOCKED /
explicit-claim_time pattern, but posts a Slack card instead of sending
an email.

Timing is strict (the DoD's own 10-minute window): a job claimed after
scheduled_for + 10 minutes has already missed the window that makes the
button meaningful and is marked SKIPPED rather than posting a stale
prompt that would only invite an incorrect late click.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.services.slack import post

_CLAIM_LEASE_MINUTES = 10
_LATE_WINDOW = timedelta(minutes=10)


def _token_secret() -> bytes:
	s = get_settings()
	if not s.no_show_token_secret:
		raise RuntimeError(
			"NO_SHOW_TOKEN_SECRET is not configured — set this env var before "
			"posting or verifying Mark No-Show prompts."
		)
	return s.no_show_token_secret.get_secret_value().encode()


def generate_no_show_token(booking_id: int) -> str:
	"""64-hex-char HMAC-SHA256 token bound to booking_id — same stateless
	pattern as src/agents/relay/resume_auth.py's halt resume token, its own
	dedicated secret so the two token families can rotate independently."""
	return hmac.new(_token_secret(), str(booking_id).encode(), hashlib.sha256).hexdigest()


def verify_no_show_token(booking_id: int, token: str) -> bool:
	try:
		expected = generate_no_show_token(booking_id)
	except RuntimeError:
		return False
	return hmac.compare_digest(expected, token if isinstance(token, str) else "")


def claim_prompts(session: Session, *, claim_time: datetime, limit: int = 20) -> List[int]:
	rows = session.execute(
		text(
			f"""
			UPDATE no_show_prompt_jobs SET status = 'SENDING', claimed_at = :claim_time
			WHERE prompt_job_id IN (
				SELECT prompt_job_id FROM no_show_prompt_jobs
				WHERE (status = 'PENDING' AND scheduled_for <= :claim_time)
				   OR (status = 'SENDING' AND claimed_at < :claim_time - INTERVAL '{_CLAIM_LEASE_MINUTES} minutes')
				ORDER BY prompt_job_id FOR UPDATE SKIP LOCKED LIMIT :limit
			)
			RETURNING prompt_job_id
			"""
		),
		{"claim_time": claim_time, "limit": limit},
	).fetchall()
	return [r.prompt_job_id for r in rows]


def _mark(session: Session, prompt_job_id: int, status: str, *, error: str = None) -> None:
	session.execute(
		text(
			"UPDATE no_show_prompt_jobs SET status = :status, last_error = :error, updated_at = NOW() "
			"WHERE prompt_job_id = :id"
		),
		{"status": status, "error": error, "id": prompt_job_id},
	)


async def send_no_show_prompt(session: Session, prompt_job_id: int, *, as_of: datetime) -> None:
	"""Called only on a job already claimed (status='SENDING') by
	claim_prompts(). Rechecks the booking's live state and the timing
	window immediately before posting, not just at claim time."""
	job = session.execute(
		text(
			"SELECT p.prompt_job_id, p.booking_id, p.scheduled_for, b.client_id, "
			"b.event_status, b.target_contact_id "
			"FROM no_show_prompt_jobs p JOIN bookings b ON p.booking_id = b.booking_id "
			"WHERE p.prompt_job_id = :id"
		),
		{"id": prompt_job_id},
	).one()

	if job.event_status == "CANCELLED":
		_mark(session, prompt_job_id, "CANCELLED")
		return

	if as_of > job.scheduled_for + _LATE_WINDOW:
		# The sweep was down, or this claim was delayed past the window
		# that matters — a "Mark No-Show" prompt posted after the DoD's
		# own 10-minute window has already closed would only invite an
		# incorrect late click. Never post it.
		_mark(session, prompt_job_id, "SKIPPED", error="claimed after scheduled_for + 10min window")
		return

	if job.target_contact_id is None:
		# Never show an actionable button for an unresolved contact —
		# pausing an inferred contact would be worse than not pausing.
		_mark(session, prompt_job_id, "BLOCKED", error="target_contact_id unresolved (booking is PENDING_RECONCILIATION)")
		return

	token = generate_no_show_token(job.booking_id)
	value = json.dumps({"booking_id": job.booking_id, "token": token}, separators=(",", ":"))
	posted = await post.post_action_card(
		channel_key="command",
		text=f":clock1: Sales demo starting now for booking `{job.booking_id}` — no-show?",
		blocks=[
			{
				"type": "section",
				"text": {"type": "mrkdwn", "text": f":clock1: Sales demo starting now for booking `{job.booking_id}` — did the prospect show?"},
			},
			{
				"type": "actions",
				"elements": [
					{"type": "button", "text": {"type": "plain_text", "text": "Mark No-Show"}, "style": "danger", "action_id": "mark_no_show", "value": value}
				],
			},
		],
	)
	if posted is None:
		_mark(session, prompt_job_id, "FAILED", error="Slack post failed or unconfigured")
		return

	session.execute(
		text(
			"UPDATE no_show_prompt_jobs SET status = 'SENT', slack_channel_id = :cid, "
			"slack_message_ts = :ts, updated_at = NOW() WHERE prompt_job_id = :id"
		),
		{"cid": posted["channel_id"], "ts": posted["message_ts"], "id": prompt_job_id},
	)
