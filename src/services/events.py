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
	# Group D / D-4: this registry entry was named "owner_score_generated"
	# and required a "county" field — but owner_visibility_sweep.py has
	# always written "owner_visibility_score_calculated" with a "county_slug"
	# field (this INSERT is raw SQL, not routed through log_event(), so the
	# mismatch was never caught by validation). daily_digest.py and
	# metrics_router.py both filtered on the never-written name, so
	# scores_generated / county_rank_reports_delivered read 0 permanently in
	# both surfaces. Renamed here to match what is actually emitted.
	"owner_visibility_score_calculated": frozenset(
		{"score_total", "county_slug", "data_coverage_pct", "county_rank"}
	),
	"meeting_outcome_recorded": frozenset(
		{"attendance_status", "pm_software", "door_count_est", "objections", "next_action"}
	),
	# Subtask 1.2.1 — Zero-Deposit Card Auth & ACH Mandate Capture.
	"payment_auth_completed": frozenset({"stripe_customer_id", "offer_code"}),
	"payment_auth_failed": frozenset({"error_code", "error_message", "offer_code"}),
	# Subtask 1.2.2 — 50/50 Settlement Split Engine & 60-Day Clawback Monitor.
	"door_signed": frozenset(
		{"pms_agreement_id", "opportunity_id", "door_count", "agreement_source", "door_signed_at"}
	),
	"settlement_opened": frozenset(
		{"transaction_id", "total_bounty_cents", "installment_1_cents",
		 "installment_2_cents", "installment_2_scheduled_for"}
	),
	"settlement_installment_1_charged": frozenset(
		{"transaction_id", "amount_cents", "rail", "stripe_invoice_id"}
	),
	"settlement_installment_2_charged": frozenset(
		{"transaction_id", "amount_cents", "rail", "stripe_invoice_id"}
	),
	"settlement_charge_failed": frozenset(
		{"transaction_id", "installment", "error_code", "error_message"}
	),
	# The blueprint's own event name (Tasks/…v2.md:604) — kept verbatim.
	"settlement_clawback_executed": frozenset(
		{"transaction_id", "installment_2_cents", "terminated_at", "days_since_signed"}
	),
	"evidence_packet_compiled": frozenset(
		{"transaction_id", "sha256", "bytes", "sections_with_gaps"}
	),
	# Task 4.2.1 — Speed-to-Lead ingest events
	"inbound_lead_received": frozenset({"source_channel", "channel", "message_id"}),
	"non_poach_suppressed": frozenset({"message_id", "source_channel"}),
	"speed_to_lead_response_sent": frozenset({"message_id", "ack_latency_seconds"}),
	"closer_alert_posted": frozenset({"message_id", "slack_channel"}),
	"context_card_generated": frozenset({"intent_class", "sla_due_at"}),
	# Subtask 1.2.3 — Six Billing Rules. "Proof ledger" (named repeatedly in
	# the Sept-04 client docs but never defined or backed by a table anywhere)
	# is interpreted as this events stream — see
	# migrations/apply_entitlements_billing.py's module docstring.
	"respond_ack_missed": frozenset({"inbound_message_id", "ack_latency_seconds", "channel"}),
	"billing_credit_issued": frozenset({"credit_id", "credit_type", "amount_cents", "billing_period"}),
	"first_sit_consumed": frozenset({"client_id", "appointment_id", "offer_code"}),
	"sixty_day_guarantee_applied": frozenset({"client_id", "attended_sit_count", "billing_period"}),
	"rate_migration_applied": frozenset({"offer_code", "new_price_cents", "clients_updated", "founding_skipped"}),
	# S-1 — Vera health gate. Written once per transition into a halting
	# state (not once per health tick — see src/tasks/vera_health_sweep.py's
	# module docstring), so this is the proof-ledger record of exactly when
	# and why settlement/billing sweeps stopped running.
	"vera_health_halt_issued": frozenset({"reason", "abstaining_checks", "ran_at"}),
	# S-8 — tracking pixel/click on the cold 5-touch sequence's Touch 1
	# (W1 §3.1.4) and the daily digest's open/click/reply rate metrics
	# (src/tasks/daily_digest.py's own "CROSS-TASK EVENT CONTRACT" comment
	# names these three event_type strings exactly). dispatch_id is required
	# on all three so a row can be joined back to the specific send it
	# measures (Evidence Packet §2) and so the pixel/click producer can
	# dedupe per dispatch (src/api/email_tracking_router.py) rather than
	# counting every re-fetch as a separate open.
	"email_opened": frozenset({"dispatch_id"}),
	"email_clicked": frozenset({"dispatch_id", "target_url"}),
	# Only ever written for a Tier-1 (message-id) attributed reply — see
	# src/services/inbound_ingest.py's producer and the plan's note on why a
	# Tier-2 (sender-email-only) match has no dispatch_id to attribute to.
	"email_replied": frozenset({"dispatch_id"}),
	# S-11 — real reply-send from the #sales-replies "Reply in Thread"/"Book
	# Meeting" cards. Deliberately its own event_type, never
	# outbound_touch_dispatched — a manual rep reply is not a cold-sequence
	# touch and must not pollute daily_digest.py's cold_emails_dispatched/
	# open/click/reply-rate denominators, which are scoped to the 5-touch
	# sequence only (see docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md).
	"sales_reply_sent": frozenset({"inbound_id", "contact_id"}),
	"sales_meeting_link_sent": frozenset({"inbound_id", "contact_id"}),
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
	# Subtask 3.2.1 — Enrichment Pipeline Wiring Verification. `provider` is
	# required because the DoD names it explicitly ("event logged with
	# provider field populated"). `signal_source` ("WINBACK_IMPORT" today,
	# "HOMESTEAD_DROP" / "SPEED_TO_LEAD" once those pipelines produce
	# signals of their own — see src/services/owner_enrichment.py's
	# enrich_homestead_drop_signals) is what makes the client's "for every
	# signal" requirement auditable per source, not just in aggregate.
	"owner_enrichment_completed": frozenset(
		{"provider", "signal_source", "email_found", "phone_found", "requires_review"}
	),
	# S-12 — UNSUBSCRIBE now also suppresses the sender's whole domain (not
	# just their email), and this event is the proof-ledger record of that
	# action. contacts.is_opted_out/suppression_state remain the GLOBAL
	# (cross-tenant) record of truth per email_suppression.py's own
	# docstring — this event is deliberately scoped to the client_id of the
	# INBOUND MESSAGE that triggered the suppression (events.client_id is
	# NOT NULL), documenting per-tenant *when a suppression was observed*,
	# not claiming the suppression itself is tenant-scoped.
	"suppression_applied": frozenset({"scope", "sender_email", "reason"}),
	# ghost_shopper_audit is deliberately ABSENT: the client spec update
	# (Tasks/Updated_client spec/Project_Blackink_Complete_Implementation_
	# Blueprint__Full__v2.md line 1264) confirms Ghost-Shopper is
	# permanently deferred, replaced by owner_visibility_score_calculated above — not
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


# event_type values whose payload's "dispatch_id" key must be unique per
# (client_id, event_type) — see already_logged_for_dispatch() and
# migrations/apply_events_dispatch_dedup_index.py's partial unique index,
# which enforces this at the database level as a backstop against the
# check-then-insert race the application-level check alone can't close.
# Code-review finding (S-8/S-11): email_opened/email_clicked originally had
# only the application-level check; email_replied had no dedup guard at
# all, letting a prospect who replies twice to the same touch double-count
# toward daily_digest.py's reply_rate_pct.
DISPATCH_DEDUPED_EVENT_TYPES = frozenset({"email_opened", "email_clicked", "email_replied"})


def already_logged_for_dispatch(session: Session, client_id: str, event_type: str, dispatch_id: str) -> bool:
	"""True if an event of this type already exists for this dispatch_id.

	Application-level pre-check shared by every DISPATCH_DEDUPED_EVENT_TYPES
	producer (src/api/email_tracking_router.py, src/services/inbound_ingest.py)
	— cheap, correct for the overwhelmingly common non-concurrent case. The
	database-level partial unique index is the real backstop for the narrow
	concurrent-hit race this check alone cannot close (two requests both
	passing this SELECT before either commits): a second INSERT that loses
	the race hits the unique constraint, which log_event() catches (its own
	documented "never raise into the caller" contract) and buffers for
	retry — the retry keeps losing the same conflict until evicted from the
	1000-item buffer. That's inert noise, not data corruption: the single
	correct row from the winning request already exists, which is what
	actually matters for the digest's correctness."""
	row = session.execute(
		text(
			"SELECT 1 FROM events WHERE client_id = :client_id AND event_type = :event_type "
			"AND payload->>'dispatch_id' = :dispatch_id LIMIT 1"
		),
		{"client_id": client_id, "event_type": event_type, "dispatch_id": dispatch_id},
	).first()
	return row is not None


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
