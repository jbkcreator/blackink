"""Three-Touch Win-Back Sequence — arming, per-touch dispatch, compliance
gate, and stop handling (Subtask 3.1.2). Consumes 3.1.1's dispositioned
`winback_rows` output; never writes to `contacts`/`sequence_runs` — per
3.1.1's own deliberate decision, winback owners stay fully standalone.

Mirrors the cold 5-touch sequence's shape (sequence_enrollment.py +
sequence_orchestrator.py + sequence_dispatcher.py combined here into one
module, since this subsystem is smaller) but against `winback_rows` /
`winback_touch_dispatches` instead of `contacts` / `sequence_runs` /
`sequence_touch_dispatches` — those tables' columns don't fit a winback row
(no company_id, no compliance_eligibility, etc.), so this is a parallel,
not a shared, gate and dispatch path. See
docs/plans/2026-09-07-subtask-3.1.2-three-touch-winback-sequence.md for the
full design rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services import work_orders as wo
from src.services.compliance_gate import PASS, GateCheckResult
from src.services.email_sender import EmailSender, build_email_sender
from src.services.email_unsubscribe import append_unsubscribe_footer, unsubscribe_url
from src.services.events import log_touch_dispatched
from src.services.mailbox_dispatcher import (
	AllMailboxesCapped,
	NoMailboxAvailable,
	get_active_mailbox_for_client,
)
from src.services.booking_link import resolve_owner_booking_link
from src.services.winback_content import render_winback_touch
from src.services.winback_ingest import STILL_OWNS_NOT_RENTING, STILL_OWNS_STILL_RENTING

logger = logging.getLogger(__name__)

# Day offsets per touch, relative to a row's own armed_at (plus the
# priority-ordering offset applied in arm_winback_run — see there).
_TOUCH_DAY_OFFSETS: dict[int, int] = {1: 0, 2: 5, 3: 12}

# Touch N threads to the reply chain of touch N-1 — same In-Reply-To
# mechanism sequence_orchestrator.py uses for the cold sequence.
_REPLY_TO_STEP: dict[int, int] = {2: 1, 3: 2}

_ARMABLE_DISPOSITIONS = (STILL_OWNS_STILL_RENTING, STILL_OWNS_NOT_RENTING)

_STOP_REASONS = ("REPLY", "OPT_OUT", "MEETING_BOOKED")


@dataclass(frozen=True)
class WinbackGateResult:
	"""Same ready/blocked_reasons shape as compliance_gate.ComplianceGateResult,
	just keyed on winback_row_id instead of contact_id — the field names
	genuinely differ, so this is a small, separate dataclass rather than
	forcing winback_row_id into a field literally named contact_id."""

	winback_row_id: int
	checks: Tuple[GateCheckResult, ...]

	@property
	def ready(self) -> bool:
		return all(c.status == PASS for c in self.checks)

	@property
	def blocked_reasons(self) -> Tuple[str, ...]:
		return tuple(f"{c.name}: {c.status} - {c.detail}" for c in self.checks if c.status != PASS)


@dataclass(frozen=True)
class WinbackTouchResult:
	winback_row_id: int
	touch_step: int
	# SENT | COMPLIANCE_BLOCK | NO_MAILBOX | VOLUME_CAP | ALREADY_CLAIMED
	# | SEND_FAILED | RECLAIMED | NO_CONTENT
	outcome: str
	message_id: str = ""


def _record_gate_check(session: Session, winback_row_id: int, client_id: str, check: GateCheckResult) -> None:
	"""Persists one check to winback_gate_checks — the audit-trail
	counterpart to compliance_gate.py's _record()/compliance_gate_checks,
	for the same reason that table gives the cold sequence: a queryable
	record of *why* a touch was or wasn't blocked, not just a log line."""
	session.execute(
		text(
			"INSERT INTO winback_gate_checks (winback_row_id, client_id, check_name, status, detail) "
			"VALUES (:winback_row_id, :client_id, :check_name, :status, :detail)"
		),
		{
			"winback_row_id": winback_row_id,
			"client_id": client_id,
			"check_name": check.name,
			"status": check.status,
			"detail": check.detail,
		},
	)


def evaluate_winback_touch_gate(session: Session, row, client_id: str) -> WinbackGateResult:
	"""Re-checked fresh from the DB at every touch dispatch (arm time AND
	sweep time — see winback_sequencer's callers) — a row can be suppressed
	or stopped after Touch 1 already sent, and Touch 2/3 must respect that.

	Deliberately does NOT re-run a DNC/non-poach vendor lookup here — 3.1.1's
	own spec places that check once, at ingest, before sequence arm; this
	gate re-reads winback_rows' own durable suppression columns, it doesn't
	re-verify them against Tracerfy/client_pm_books on every touch.

	Every check is recorded to winback_gate_checks (not just evaluated) —
	same "always evaluate and log everything, never short-circuit the audit
	trail" posture compliance_gate.py's own docstring states, so a later
	"why was this touch blocked" question has a queryable answer rather than
	only a log line."""
	checks = (
		_check_disposition(row),
		_check_not_suppressed(row),
		_check_not_stopped(row),
		_check_has_email(row),
	)
	for check in checks:
		_record_gate_check(session, row.winback_row_id, client_id, check)
	return WinbackGateResult(winback_row_id=row.winback_row_id, checks=checks)


def _check_disposition(row) -> GateCheckResult:
	if row.disposition not in _ARMABLE_DISPOSITIONS:
		return GateCheckResult("disposition", "FAIL", f"disposition={row.disposition}")
	return GateCheckResult("disposition", PASS, f"disposition={row.disposition}")


def _check_not_suppressed(row) -> GateCheckResult:
	if row.suppression_state:
		return GateCheckResult("not_suppressed", "FAIL", f"suppression_reason={row.suppression_reason}")
	return GateCheckResult("not_suppressed", PASS, "suppression_state=false")


def _check_not_stopped(row) -> GateCheckResult:
	if row.stopped_at is not None:
		return GateCheckResult("not_stopped", "FAIL", f"stop_reason={row.stop_reason}")
	return GateCheckResult("not_stopped", PASS, "stopped_at=null")


def _check_has_email(row) -> GateCheckResult:
	if not row.email:
		return GateCheckResult("has_email", "FAIL", "email is null")
	return GateCheckResult("has_email", PASS, "email present")


def stop_active_winback_runs(session: Session, client_id: str, email: str, reason: str) -> int:
	"""Stop every not-yet-stopped winback_rows run for this (client_id, email).

	Idempotent — a second reply/opt-out/booking for the same owner is a
	no-op, not an error (WHERE stopped_at IS NULL). Does not cancel already-
	QUEUED agent_work_orders rows outright; evaluate_winback_touch_gate
	catches the stop at dispatch/sweep time instead — one fewer moving part,
	same guarantee. Returns the number of rows stopped (0 if none matched —
	a reply from an email that isn't in winback_rows at all for this client
	is a normal, expected no-op, not an error)."""
	if reason not in _STOP_REASONS:
		raise ValueError(f"stop_active_winback_runs: {reason!r} is not a recognized stop_reason")
	result = session.execute(
		text(
			"UPDATE winback_rows SET stopped_at = NOW(), stop_reason = :reason, updated_at = NOW() "
			"WHERE client_id = :client_id AND lower(email) = lower(:email) AND stopped_at IS NULL"
		),
		{"client_id": client_id, "email": email, "reason": reason},
	)
	if result.rowcount:
		logger.info(
			"winback_sequencer: stopped %d run(s) client_id=%s email=%s reason=%s",
			result.rowcount, client_id, email, reason,
		)
	return result.rowcount


def _resolve_county_name(session: Session, county_slug: Optional[str]) -> str:
	if not county_slug:
		return "your area"
	row = session.execute(
		text("SELECT county_name FROM counties WHERE county_slug = :slug"),
		{"slug": county_slug},
	).first()
	return row.county_name if row else "your area"


def arm_winback_run(
	session: Session,
	client_id: str,
	row,
	armed_at: datetime,
) -> list[str]:
	"""Enqueue all 3 touches for one winback_rows row. Returns the list of
	action_ids enqueued (empty if the row isn't in an armable disposition —
	callers should skip a row rather than treat that as an error, since a
	bulk 'arm this import' call legitimately spans SOLD/UNKNOWN rows too).

	Priority ordering (the DoD's own mechanism, not a separate scheduler):
	STILL_OWNS_STILL_RENTING rows get base_offset=0 days; STILL_OWNS_NOT_RENTING
	rows get base_offset=1 day. Because the sweep only ever posts a card once
	due_at has arrived, this single offset guarantees no NOT_RENTING Touch 1
	card appears before every STILL_RENTING Touch 1 card was already queued.
	"""
	if row.disposition not in _ARMABLE_DISPOSITIONS:
		logger.warning(
			"arm_winback_run: winback_row_id=%s disposition=%s is not armable — skipped",
			row.winback_row_id, row.disposition,
		)
		return []
	if not row.email:
		logger.warning("arm_winback_run: winback_row_id=%s has no email — skipped", row.winback_row_id)
		return []

	base_offset = timedelta(days=0) if row.disposition == STILL_OWNS_STILL_RENTING else timedelta(days=1)
	county_name = _resolve_county_name(session, row.county_slug)
	# Resolved per-row (not once for the whole batch) so a GOHIGHLEVEL
	# booking page prefills with THIS owner's own name/email — see
	# resolve_owner_booking_link's own docstring.
	booking_link = resolve_owner_booking_link(session, client_id, name=row.owner_name, email=row.email)
	booking_url = booking_link.url if booking_link else None

	action_ids: list[str] = []
	for touch_step, day_offset in _TOUCH_DAY_OFFSETS.items():
		due_at = armed_at + base_offset + timedelta(days=day_offset)
		subject, body, template_version = render_winback_touch(
			touch_step,
			owner_name=row.owner_name,
			county_name=county_name,
			property_address=row.property_address_raw,
			audit_loss_dollars_est=row.audit_loss_dollars_est,
			booking_url=booking_url,
		)
		payload = {
			"winback_row_id": row.winback_row_id,
			"touch_step": touch_step,
			"subject": subject,
			"body": body,
			"template_version": template_version,
		}
		idempotency_key = f"winback:{row.winback_row_id}:touch:{touch_step}"
		order = wo.enqueue(
			client_id=client_id,
			entity_type="winback_row",
			entity_id=str(row.winback_row_id),
			agent_id="winback_sequencer",
			action_class="DISPATCH_WINBACK_TOUCH",
			autonomy_band="BAND_2_ONE_TAP",
			risk_class="LOW",
			recipient=row.email,
			payload=payload,
			config_fingerprint={"channel": "winback", "winback_row_id": row.winback_row_id, "touch_step": touch_step},
			idempotency_key=idempotency_key,
			due_at=due_at,
		)
		action_ids.append(order.action_id)

	logger.info(
		"arm_winback_run: winback_row_id=%s client_id=%s disposition=%s armed %d touch(es)",
		row.winback_row_id, client_id, row.disposition, len(action_ids),
	)
	return action_ids


def _reassert_tenant(session: Session, client_id: str) -> None:
	if client_id:
		session.execute(text("SET LOCAL app.current_client_id = :cid"), {"cid": client_id})


def _claim_touch(session: Session, client_id: str, winback_row_id: int, touch_step: int, mailbox_id: int) -> Optional[str]:
	row = session.execute(
		text(
			"INSERT INTO winback_touch_dispatches "
			"(client_id, winback_row_id, touch_step, mailbox_id, status) "
			"VALUES (:client_id, :winback_row_id, :touch_step, :mailbox_id, 'SENDING') "
			"ON CONFLICT (winback_row_id, touch_step) DO NOTHING "
			"RETURNING dispatch_id"
		),
		{"client_id": client_id, "winback_row_id": winback_row_id, "touch_step": touch_step, "mailbox_id": mailbox_id},
	).fetchone()
	if row is None:
		logger.warning(
			"winback_sequencer: touch already claimed winback_row_id=%s touch_step=%d",
			winback_row_id, touch_step,
		)
		return None
	return str(row.dispatch_id)


def _mark_sent(session: Session, client_id: str, dispatch_id: str, message_id: str) -> bool:
	result = session.execute(
		text(
			"UPDATE winback_touch_dispatches "
			"SET status = 'SENT', message_id = :message_id, sent_at = :now, updated_at = :now "
			"WHERE dispatch_id = :dispatch_id AND client_id = :client_id AND status = 'SENDING'"
		),
		{"message_id": message_id, "now": datetime.now(timezone.utc), "dispatch_id": dispatch_id, "client_id": client_id},
	)
	return result.rowcount > 0


def _mark_failed(session: Session, client_id: str, dispatch_id: str, reason: str) -> None:
	session.execute(
		text(
			"UPDATE winback_touch_dispatches SET status = 'FAILED', updated_at = :now "
			"WHERE dispatch_id = :dispatch_id AND client_id = :client_id"
		),
		{"now": datetime.now(timezone.utc), "dispatch_id": dispatch_id, "client_id": client_id},
	)
	logger.warning("winback_sequencer: dispatch_id=%s FAILED reason=%s", dispatch_id, reason)


def _get_touch_message_id(session: Session, winback_row_id: int, touch_step: int) -> Optional[str]:
	row = session.execute(
		text(
			"SELECT message_id FROM winback_touch_dispatches "
			"WHERE winback_row_id = :id AND touch_step = :step AND status = 'SENT'"
		),
		{"id": winback_row_id, "step": touch_step},
	).fetchone()
	return row.message_id if row else None


def dispatch_winback_touch(
	session: Session,
	row,
	client_id: str,
	touch_step: int,
	sender: EmailSender | None = None,
	*,
	subject: str | None = None,
	body: str | None = None,
	template_version: str = "",
) -> WinbackTouchResult:
	"""Run the gate, pick an under-cap mailbox, claim the at-most-once slot,
	send, resolve SENDING -> SENT/FAILED. Mirrors sequence_orchestrator.dispatch_touch
	structurally — see that function's own docstring for the durability-boundary
	rationale (claim committed before the external send, in its own transaction)."""
	sender = sender or build_email_sender()
	winback_row_id = row.winback_row_id

	if not subject or not body:
		logger.error(
			"winback_sequencer: NO_CONTENT winback_row_id=%s touch=%d — approved subject/body absent",
			winback_row_id, touch_step,
		)
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="NO_CONTENT")

	gate = evaluate_winback_touch_gate(session, row, client_id)
	if not gate.ready:
		logger.info(
			"winback_sequencer: compliance block winback_row_id=%s touch=%d reasons=%s",
			winback_row_id, touch_step, gate.blocked_reasons,
		)
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="COMPLIANCE_BLOCK")

	try:
		mailbox = get_active_mailbox_for_client(session, client_id)
	except AllMailboxesCapped as exc:
		logger.info("winback_sequencer: all mailboxes capped for client_id=%s — deferring: %s", client_id, exc)
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="VOLUME_CAP")
	except NoMailboxAvailable as exc:
		logger.error("winback_sequencer: no mailbox for client_id=%s — %s", client_id, exc)
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="NO_MAILBOX")

	in_reply_to: Optional[str] = None
	if touch_step in _REPLY_TO_STEP:
		in_reply_to = _get_touch_message_id(session, winback_row_id, _REPLY_TO_STEP[touch_step])

	dispatch_id = _claim_touch(session, client_id, winback_row_id, touch_step, mailbox.mailbox_id)
	if dispatch_id is None:
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="ALREADY_CLAIMED")

	# Durability boundary — see sequence_orchestrator.dispatch_touch's own
	# docstring for the full rationale: commit the SENDING claim before the
	# external send, in its own transaction.
	session.commit()
	_reassert_tenant(session, client_id)

	unsub_url = unsubscribe_url(client_id, row.email)
	body_with_footer = append_unsubscribe_footer(body, unsub_url)

	try:
		result = sender.send(
			from_address=mailbox.mailbox_address,
			to_address=row.email,
			subject=subject,
			body=body_with_footer,
			sending_domain=mailbox.sending_domain,
			in_reply_to=in_reply_to,
			list_unsubscribe_url=unsub_url,
		)
	except Exception as exc:  # noqa: BLE001 — any send failure resolves the row
		_mark_failed(session, client_id, dispatch_id, str(exc))
		logger.error(
			"winback_sequencer: send FAILED winback_row_id=%s touch=%d dispatch=%s: %s",
			winback_row_id, touch_step, dispatch_id, exc,
		)
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="SEND_FAILED")

	if not _mark_sent(session, client_id, dispatch_id, result.message_id):
		return WinbackTouchResult(winback_row_id=winback_row_id, touch_step=touch_step, outcome="RECLAIMED")

	log_touch_dispatched(
		session=session,
		client_id=client_id,
		contact_id=winback_row_id,
		touch_step=touch_step,
		dispatch_id=dispatch_id,
		mailbox_id=mailbox.mailbox_id,
		sending_domain=mailbox.sending_domain,
		template_version=template_version,
		recipient_email=row.email,
		campaign_type="WIN_BACK",
	)

	return WinbackTouchResult(
		winback_row_id=winback_row_id, touch_step=touch_step, outcome="SENT", message_id=result.message_id,
	)
