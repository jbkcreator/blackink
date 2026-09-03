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

	with get_db_context(client_id=order.client_id) as session:
		row = session.execute(
			text("SELECT * FROM contacts WHERE contact_id = :cid"),
			{"cid": contact_id},
		).fetchone()
		if row is None:
			logger.error("dispatch_email_touch: contact_id=%s not found", contact_id)
			return {"outcome": "CONTACT_NOT_FOUND", "contact_id": contact_id}

		result = dispatch_touch(session, row, order.client_id, touch_step=touch_step, run_id=run_id)

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
	# VOLUME_CAP is transient and self-healing — signal the sweep to DEFER the
	# order (SNOOZED + pushed due_at) instead of finalising it (ticket 08).
	if result.outcome == "VOLUME_CAP":
		receipt["defer"] = True
	return receipt


DISPATCHERS: Dict[str, Callable[[WorkOrder], dict]] = {
	"noop": noop_dispatch,
	"setter": dispatch_email_touch,
}
