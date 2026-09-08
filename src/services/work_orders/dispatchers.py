"""Execution dispatchers for APPROVED work orders — Dev 3 plan §3, item 1
("dispatchers"). Week 0 registers only `noop`: there is no real email/SMS
dispatch channel yet (that lands with the Week 1 Campaign Agent). Its job
is narrower but real — proving the state machine QUEUED -> APPROVED ->
EXECUTING -> DONE actually closes end to end, which is what AC #1's demo
needs to show.

DISPATCHERS is a plain dict, not a class registry, matching
FA/src/services/relay/channels.py's DISPATCHERS shape (referenced in the
Dev 3 plan's engine.py fork notes) — one lookup table, new channels added
by adding an entry, not subclassing anything.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict

from src.services.work_orders import WorkOrder

logger = logging.getLogger(__name__)


def noop_dispatch(order: WorkOrder) -> dict:
	"""Does nothing except prove the pipeline reaches this point. Returns
	the execution_receipt payload the caller persists via record_execution."""
	logger.info("[dispatchers] noop dispatch — action_id=%s action_class=%s", order.action_id, order.action_class)
	return {"dispatcher": "noop", "note": "Week 0 — no real channel registered yet"}


def dispatch_email_touch(order: WorkOrder) -> dict:
	"""DISPATCH_EMAIL_TOUCH — runs compliance, picks mailbox, claims at-most-once slot.

	Pulls run_id and touch_step from order.payload (set by enroll_contact).
	Fetches the contact from DB under the client's RLS session, then calls
	sequence_orchestrator.dispatch_touch for the real compliance/mailbox/claim path.
	"""
	from sqlalchemy import text

	from src.core.database import get_db_context
	from src.services.sequence_orchestrator import dispatch_touch

	run_id = order.payload.get("run_id", "")
	touch_step = int(order.payload.get("touch_step", 0))
	contact_id = int(order.entity_id)
	subject = order.payload.get("subject")
	body = order.payload.get("body")
	template_version = order.payload.get("template_version", "")

	with get_db_context(client_id=order.client_id) as session:
		row = session.execute(
			text("SELECT * FROM contacts WHERE contact_id = :cid"),
			{"cid": contact_id},
		).fetchone()
		if row is None:
			logger.error("dispatch_email_touch: contact_id=%s not found", contact_id)
			return {"outcome": "CONTACT_NOT_FOUND", "contact_id": contact_id, "fail": True}

		result = dispatch_touch(
			session, row, order.client_id, touch_step=touch_step, run_id=run_id,
			subject=subject, body=body, template_version=template_version,
		)

	logger.info(
		"dispatch_email_touch: action_id=%s contact_id=%s touch=%d outcome=%s",
		order.action_id, contact_id, touch_step, result.outcome,
	)
	receipt = {
		"outcome": result.outcome,
		"contact_id": contact_id,
		"touch_step": touch_step,
		"run_id": run_id,
		"message_id": result.message_id,
	}
	# Explicit outcome routing (finding #3) — only SENT (and the terminal
	# no-send verdicts COMPLIANCE_BLOCK/ALREADY_CLAIMED) may become DONE. Every
	# other outcome must NOT be silently finalised as success:
	#   defer  → transient/self-healing availability; SNOOZE + retry later.
	#   fail   → send failed or ambiguous; route to FAILED + #blackink-qa alert
	#            for manual reconciliation (never drop the touch silently).
	if result.outcome in _DEFER_OUTCOMES:
		receipt["defer"] = True
	elif result.outcome in _FAIL_OUTCOMES:
		receipt["fail"] = True
	return receipt


def dispatch_winback_touch(order: WorkOrder) -> dict:
	"""DISPATCH_WINBACK_TOUCH — Subtask 3.1.2. Same shape as
	dispatch_email_touch above, but against winback_rows/winback_sequencer
	instead of contacts/sequence_orchestrator — winback owners are never
	linked to contacts (see 3.1.1's own deliberate decision)."""
	from sqlalchemy import text

	from src.core.database import get_db_context
	from src.services.winback_sequencer import dispatch_winback_touch as _dispatch

	touch_step = int(order.payload.get("touch_step", 0))
	winback_row_id = int(order.entity_id)
	subject = order.payload.get("subject")
	body = order.payload.get("body")
	template_version = order.payload.get("template_version", "")

	with get_db_context(client_id=order.client_id) as session:
		row = session.execute(
			text("SELECT * FROM winback_rows WHERE winback_row_id = :id"),
			{"id": winback_row_id},
		).fetchone()
		if row is None:
			logger.error("dispatch_winback_touch: winback_row_id=%s not found", winback_row_id)
			return {"outcome": "WINBACK_ROW_NOT_FOUND", "winback_row_id": winback_row_id, "fail": True}

		result = _dispatch(
			session, row, order.client_id, touch_step=touch_step,
			subject=subject, body=body, template_version=template_version,
		)

	logger.info(
		"dispatch_winback_touch: action_id=%s winback_row_id=%s touch=%d outcome=%s",
		order.action_id, winback_row_id, touch_step, result.outcome,
	)
	receipt = {
		"outcome": result.outcome,
		"winback_row_id": winback_row_id,
		"touch_step": touch_step,
		"message_id": result.message_id,
	}
	if result.outcome in _DEFER_OUTCOMES:
		receipt["defer"] = True
	elif result.outcome in _FAIL_OUTCOMES:
		receipt["fail"] = True
	return receipt


def dispatch_manual_task(order: WorkOrder) -> dict:
	"""DIAL_TASK / LINKEDIN_TASK — human-performed touches (phone, LinkedIn).

	There is no automated channel: the sequence_sweep posts the card to the
	relevant Slack channel, a human performs the touch offline, and Approving
	the card lands here. This dispatcher only records completion — it does not
	send anything — closing the state machine so the row cannot sit QUEUED
	forever (finding #4)."""
	logger.info(
		"[dispatchers] manual task acknowledged action_id=%s action_class=%s contact=%s",
		order.action_id, order.action_class, order.entity_id,
	)
	return {"dispatcher": "manual", "action_class": order.action_class, "note": "manual touch acknowledged"}


# Outcomes from dispatch_touch that must NOT finalise a work order as DONE.
_DEFER_OUTCOMES = {"VOLUME_CAP", "NO_MAILBOX"}          # transient availability — retry
_FAIL_OUTCOMES = {"SEND_FAILED", "RECLAIMED", "NO_CONTENT"}  # ambiguous/failed — reconcile

DISPATCHERS: Dict[str, Callable[[WorkOrder], dict]] = {
	"noop": noop_dispatch,
	"setter": dispatch_email_touch,
	"manual": dispatch_manual_task,
	"winback": dispatch_winback_touch,
}
