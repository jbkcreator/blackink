"""EventLogger — the single write path for the `events` table.

Every INSERT into events, anywhere in the codebase, MUST go through
log_event(). Two call sites pre-dated this module and wrote raw SQL
directly (src/services/slack/listeners.py, src/tasks/promotion_sweep.py);
both are refactored onto this in the same change that adds it — leaving
either as "the old way" would mean two write paths to keep in sync with
whatever validation this module adds later.

Written against the ACTUAL events schema (migrations/apply_events.py):
id BIGSERIAL, client_id, event_type, entity_type, entity_id, payload
JSONB, actor, created_at. This does NOT match the blueprint's illustrative
events DDL (owner_id/campaign_id/value_cents/source/occurred_at) — see
Company's own docstring in src/core/models.py for the established
precedent that the real schema wins over the blueprint's prose/SQL when
they disagree.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import text

from src.core.database import get_db_context

logger = logging.getLogger(__name__)


class MalformedEventError(ValueError):
	"""Raised when a payload is missing a required field for its event_type.
	The event is NOT written — never silently dropped, never written partial."""


# event_type -> required payload keys. Extend this as new event types are
# introduced; an event_type with no entry here has no required fields.
REQUIRED_PAYLOAD_FIELDS: dict[str, frozenset] = {
	"outbound_touch_dispatched": frozenset(
		{"touch_step", "channel", "recipient_email", "template_version", "sending_domain", "mailbox_id"}
	),
	"ghost_shopper_audit": frozenset({"ghost_shopper_submitted_at", "target_domain"}),
	"meeting_outcome_recorded": frozenset(
		{"attendance_status", "pm_software", "door_count_est", "objections", "next_action"}
	),
	# The rentbot_* event types (rentbot_address_received, rentbot_fallback_used,
	# rentbot_demo_optin, rentbot_keyword_received) are deliberately ABSENT:
	# the Rent Analysis Bot is deferred to Q1 by client instruction, so nothing
	# writes them yet and a registry entry for an event no code emits is dead
	# config. They are listed in the plan's Appendix A and get added together
	# with the code that writes them.
}

# In-memory retry buffer for writes issued while the DB is unavailable.
# ponytail: process-local list, lost on restart — a durable outbox (Redis
# list or a WAL file) is the upgrade path if DB outages start outlasting a
# single process's uptime. Bounded at 1000 per the split doc's spec; past
# that, oldest events are dropped with a WARNING log (never silently, and
# never by raising into the caller's request path).
_MAX_BUFFER = 1000
_pending_buffer: list[dict] = []


def _required_fields_ok(event_type: str, payload: dict) -> None:
	required = REQUIRED_PAYLOAD_FIELDS.get(event_type)
	if not required:
		return
	missing = required - payload.keys()
	if missing:
		raise MalformedEventError(f"event_type={event_type!r} missing required payload fields: {sorted(missing)}")


def _insert(client_id: str, event_type: str, entity_type: str, entity_id: str, payload: dict, actor: Optional[str]) -> None:
	with get_db_context(client_id=client_id) as session:
		session.execute(
			text(
				"INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
				"VALUES (:client_id, :event_type, :entity_type, :entity_id, :actor, :payload)"
			),
			{
				"client_id": client_id,
				"event_type": event_type,
				"entity_type": entity_type,
				"entity_id": entity_id,
				"actor": actor,
				"payload": json.dumps(payload),
			},
		)


def log_event(
	client_id: str,
	event_type: str,
	*,
	entity_type: str,
	entity_id: str,
	payload: dict,
	actor: Optional[str] = None,
) -> None:
	"""Write one row to `events`. Raises MalformedEventError (event NOT
	written) if a required field for this event_type is missing. On a DB
	write failure, buffers the event in memory and re-raises nothing —
	callers must not have their own business logic fail because logging
	did; flush_pending() drains the buffer once the DB is reachable again."""
	_required_fields_ok(event_type, payload)
	try:
		_insert(client_id, event_type, entity_type, entity_id, payload, actor)
	except Exception:
		logger.error("[events] write failed, buffering (event_type=%s)", event_type, exc_info=True)
		if len(_pending_buffer) >= _MAX_BUFFER:
			dropped = _pending_buffer.pop(0)
			logger.warning("[events] buffer full — dropped oldest pending event_type=%s", dropped.get("event_type"))
		_pending_buffer.append(
			{
				"client_id": client_id,
				"event_type": event_type,
				"entity_type": entity_type,
				"entity_id": entity_id,
				"payload": payload,
				"actor": actor,
			}
		)


def flush_pending() -> int:
	"""Retries every buffered event. Returns the number successfully
	flushed. Called at the top of src/tasks/daily_digest.py::main() (the
	one guaranteed-scheduled process in the system) and safe to call from
	any other task worker loop — no background thread is started for this
	(ponytail: no scheduler dependency; a caller that never runs never
	flushes, which is fine since the buffer is best-effort, not a
	durability guarantee)."""
	flushed = 0
	still_pending: list[dict] = []
	for event in _pending_buffer:
		try:
			_insert(event["client_id"], event["event_type"], event["entity_type"], event["entity_id"], event["payload"], event["actor"])
			flushed += 1
		except Exception:
			still_pending.append(event)
	_pending_buffer[:] = still_pending
	return flushed
