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
from sqlalchemy.orm import Session

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
	"owner_score_generated": frozenset(
		{"score_total", "county", "data_coverage_pct", "county_rank"}
	),
	"meeting_outcome_recorded": frozenset(
		{"attendance_status", "pm_software", "door_count_est", "objections", "next_action"}
	),
	# Subtask 3.1.1 — Lost-Owner CSV Ingest. Per-disposition-bucket counts,
	# not just a total, so the proof-ledger-style visibility the spec asks
	# for ("winback_import_completed event written with row counts for each
	# disposition bucket") is enforced at write time, not left to convention.
	"winback_import_completed": frozenset(
		{
			"total_rows",
			"still_owns_still_renting_count",
			"still_owns_not_renting_count",
			"sold_count",
			"unknown_count",
			"suppressed_count",
		}
	),
	# ghost_shopper_audit is deliberately ABSENT: the client spec update
	# (Tasks/Updated_client spec/Project_Blackink_Complete_Implementation_
	# Blueprint__Full__v2.md line 1264) confirms Ghost-Shopper is
	# permanently deferred, replaced by owner_score_generated above — not
	# "on hold", genuinely never coming back. A registry entry for an
	# event type nothing will ever emit is dead config, same reasoning as
	# the rentbot_* entries below.
	#
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


def _execute_insert(session: Session, client_id: str, event_type: str, entity_type: str, entity_id: str, payload: dict, actor: Optional[str]) -> None:
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


def _insert(client_id: str, event_type: str, entity_type: str, entity_id: str, payload: dict, actor: Optional[str], session: Optional[Session] = None) -> None:
	if session is not None:
		# Caller's transaction owns the commit and the DB role — no new
		# get_db_context(), no commit here. See log_event's docstring.
		_execute_insert(session, client_id, event_type, entity_type, entity_id, payload, actor)
		return
	with get_db_context(client_id=client_id) as new_session:
		_execute_insert(new_session, client_id, event_type, entity_type, entity_id, payload, actor)


def log_event(
	client_id: str,
	event_type: str,
	*,
	entity_type: str,
	entity_id: str,
	payload: dict,
	actor: Optional[str] = None,
	session: Optional[Session] = None,
) -> None:
	"""Write one row to `events`. Raises MalformedEventError (event NOT
	written) if a required field for this event_type is missing. On a DB
	write failure, buffers the event in memory and re-raises nothing —
	callers must not have their own business logic fail because logging
	did; flush_pending() drains the buffer once the DB is reachable again.

	If `session` is provided, the event is written into that session (no
	new get_db_context(), no commit — the caller's own transaction owns
	the commit and the DB role) — for a caller like promotion_sweep that
	must have this write share its own already-open system-role
	transaction. If `session` is None (the default, and every other
	current call site), behavior is unchanged from before this parameter
	existed."""
	_required_fields_ok(event_type, payload)
	try:
		_insert(client_id, event_type, entity_type, entity_id, payload, actor, session=session)
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
	one guaranteed-scheduled process in the system), and periodically from
	within src/api/main.py's lifespan (a background loop in the long-running
	API process — the buffer is process-local, so daily_digest's own call
	can only ever drain what THAT process buffered, never what the API
	process did; the API process must drain its own). Safe to call from any
	other task worker loop too. ponytail: neither caller survives a process
	crash/restart — the buffer is in-memory, not a durable outbox (Redis
	list or a WAL file is the upgrade path if outages start outlasting a
	process's uptime); a caller that never runs never flushes, which is
	fine since the buffer is best-effort, not a durability guarantee."""
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


def log_touch_dispatched(
	session: Session,
	client_id: str,
	contact_id: int,
	touch_step: int,
	dispatch_id: str,
	mailbox_id: int,
	sending_domain: str,
	template_version: str,
	recipient_email: str,
	campaign_type: str = "COLD_OUTBOUND",
) -> None:
	"""Log an outbound_touch_dispatched event via log_event() — the single
	write path (see module docstring). entity is the contact
	(entity_type='contact', entity_id=contact_id) and actor is
	'cold_outbound_sequencer' per wayfinder ticket 03. dispatch_id rides
	along in the payload as a non-required extra field for traceability
	back to the sequence_touch_dispatches row that produced this event.

	campaign_type (Subtask 3.1.2): an optional payload field, not a new
	REQUIRED_PAYLOAD_FIELDS entry — existing cold-sequence callers need no
	change. src/services/winback_sequencer.py passes campaign_type='WIN_BACK'
	and contact_id=winback_row_id (there is no `contacts` row for a win-back
	owner; entity_type stays 'contact' as a loose "id of the thing this
	touch was sent to" label, matching the existing shape rather than adding
	a second entity_type this payload's own consumers don't expect)."""
	payload = {
		"touch_step": touch_step,
		"channel": "email",
		"recipient_email": recipient_email,
		"dispatch_id": dispatch_id,
		"mailbox_id": mailbox_id,
		"sending_domain": sending_domain,
		"template_version": template_version,
		"campaign_type": campaign_type,
	}
	log_event(
		client_id,
		"outbound_touch_dispatched",
		entity_type="contact",
		entity_id=str(contact_id),
		payload=payload,
		actor="cold_outbound_sequencer",
		session=session,
	)
	logger.info(
		"events: outbound_touch_dispatched client=%s contact=%s touch=%d dispatch=%s campaign_type=%s",
		client_id, contact_id, touch_step, dispatch_id, campaign_type,
	)
