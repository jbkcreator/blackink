"""agent_work_orders — the single read/write seam (Dev 3 plan §3.3).

REFACTOR fork of FA/src/services/relay/queue.py: venture_key -> client_id
(required, never defaulted — every function here takes client_id and scopes
its session to it, so RLS actually applies); relay_approval_queue ->
agent_work_orders; status vocabulary widened per the migration
(migrations/apply_agent_work_orders.py).

ORDERING IN enqueue() IS NOT THE OBVIOUS ONE:
  1. action_id = str(uuid.uuid4())   -- in app code, NOT the DB default
  2. payload_hash = payload_hash.compute(...) over the FULLY POPULATED
     preimage (action_id included -- hence step 1 must come first)
  3. INSERT both
This is forced by src.services.slack.payload_hash's preimage including
action_id: the digest cannot be computed before the row exists if the row
assigns its own id, and it cannot be inserted without a digest.

Per CLAUDE.md: all reads/writes use sqlalchemy.text() with named binds,
never the ORM query API.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.core.database import get_db_context, get_system_db_context
from src.services.slack import payload_hash as _hash


@dataclass(frozen=True)
class WorkOrder:
	"""Read-only view of one agent_work_orders row."""

	action_id: str
	client_id: str
	entity_type: str
	entity_id: str
	opportunity_id: Optional[str]
	agent_id: str
	action_class: str
	autonomy_band: str
	risk_class: str
	confidence_score: Optional[Decimal]
	recipient: Optional[str]
	payload: dict
	config_fingerprint: dict
	payload_hash: str
	hash_version: int
	status: str
	idempotency_key: str
	slack_channel_id: Optional[str]
	slack_message_ts: Optional[str]
	decided_by: Optional[str]
	decided_at: Optional[datetime]
	execution_receipt: Optional[dict]
	error: Optional[str]
	due_at: Optional[datetime]
	created_at: datetime
	updated_at: datetime


_ORDER_COLUMNS = tuple(f.name for f in fields(WorkOrder))
_COLUMNS_SQL = ", ".join(_ORDER_COLUMNS)


def _row_to_order(row: dict) -> WorkOrder:
	"""psycopg2 deserializes a native Postgres UUID column to a
	uuid.UUID object, not a str — confirmed against the real server
	(WorkOrder.action_id is typed str, and code throughout this module and
	src.services.slack.listeners relies on that, e.g. action_id[:8]
	slicing, which a uuid.UUID does not support). Coerced once, here, at
	the single point every row becomes a WorkOrder, rather than scattering
	str(...) calls across every caller that touches action_id."""
	values = {col: row[col] for col in _ORDER_COLUMNS}
	values["action_id"] = str(values["action_id"])
	return WorkOrder(**values)


@dataclass
class _HashInput:
	"""Exposes exactly the attributes src.services.slack.payload_hash.compute()
	reads (client_id, action_id, action_class, entity_type, entity_id,
	recipient, payload, config_fingerprint) — nothing else, since the
	preimage never touches any other field. Used to compute a digest
	before a row exists (enqueue) or before an UPDATE is issued
	(update_payload), without constructing a full WorkOrder that would
	need fields (status, timestamps, ...) that aren't known yet."""

	client_id: str
	action_id: str
	action_class: str
	entity_type: str
	entity_id: str
	recipient: Optional[str]
	payload: dict
	config_fingerprint: dict


def default_idempotency_key(client_id: str, entity_id: str, action_class: str, now: datetime) -> str:
	"""Fallback per Dev 3 plan §3.3: sha256(client_id|entity_id|action_class|
	hour-bucket). Callers with a natural key (a campaign step, a reply id)
	should pass their own idempotency_key to enqueue() instead — this is
	the fallback, not a mandate."""
	bucket = now.strftime("%Y%m%d%H")
	raw = f"{client_id}|{entity_id}|{action_class}|{bucket}"
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enqueue(
	*,
	client_id: str,
	entity_type: str,
	entity_id: str,
	agent_id: str,
	action_class: str,
	autonomy_band: str,
	risk_class: str,
	payload: dict,
	config_fingerprint: dict,
	idempotency_key: str,
	recipient: Optional[str] = None,
	opportunity_id: Optional[str] = None,
	confidence_score: Optional[Decimal] = None,
	due_at: Optional[datetime] = None,
) -> WorkOrder:
	"""Write a new QUEUED row. If (client_id, idempotency_key) already
	exists, returns the existing row instead of raising or duplicating —
	same idempotent-on-conflict shape as FA/relay/queue.py:enqueue()."""
	action_id = str(uuid.uuid4())

	digest = _hash.compute(
		_HashInput(
			client_id=client_id,
			action_id=action_id,
			action_class=action_class,
			entity_type=entity_type,
			entity_id=entity_id,
			recipient=recipient,
			payload=payload,
			config_fingerprint=config_fingerprint,
		)
	)  # type: ignore[arg-type]

	try:
		with get_db_context(client_id=client_id) as session:
			session.execute(
				text(
					f"""
					INSERT INTO agent_work_orders
						(action_id, client_id, entity_type, entity_id, opportunity_id,
						 agent_id, action_class, autonomy_band, risk_class,
						 confidence_score, recipient, payload, config_fingerprint,
						 payload_hash, hash_version, idempotency_key, due_at)
					VALUES
						(:action_id, :client_id, :entity_type, :entity_id, :opportunity_id,
						 :agent_id, :action_class, :autonomy_band, :risk_class,
						 :confidence_score, :recipient, :payload, :config_fingerprint,
						 :payload_hash, :hash_version, :idempotency_key, :due_at)
					"""
				),
				{
					"action_id": action_id,
					"client_id": client_id,
					"entity_type": entity_type,
					"entity_id": entity_id,
					"opportunity_id": opportunity_id,
					"agent_id": agent_id,
					"action_class": action_class,
					"autonomy_band": autonomy_band,
					"risk_class": risk_class,
					"confidence_score": confidence_score,
					"recipient": recipient,
					"payload": json.dumps(payload),
					"config_fingerprint": json.dumps(config_fingerprint),
					"payload_hash": digest,
					"hash_version": _hash.HASH_VERSION,
					"idempotency_key": idempotency_key,
					"due_at": due_at,
				},
			)
	except IntegrityError:
		existing = get_by_idempotency_key(client_id, idempotency_key)
		if existing is not None:
			return existing
		raise

	order = get(client_id, action_id)
	assert order is not None  # just inserted in the same call
	return order


def get(client_id: str, action_id: str) -> Optional[WorkOrder]:
	"""client_id is filtered explicitly in SQL, not left to RLS alone —
	defense-in-depth, matching every write function below. RLS
	(session_scope's SET LOCAL app.current_client_id) is still the
	non-negotiable backstop per CLAUDE.md; this filter means a lookup
	fails closed the same way even in the (should-never-happen) case
	RLS itself were misconfigured on this table."""
	with get_db_context(client_id=client_id) as session:
		row = session.execute(
			text(
				"SELECT " + _COLUMNS_SQL + " FROM agent_work_orders "
				"WHERE action_id = :action_id AND client_id = :client_id"
			),
			{"action_id": action_id, "client_id": client_id},
		).mappings().first()
		return _row_to_order(dict(row)) if row else None


def get_by_idempotency_key(client_id: str, idempotency_key: str) -> Optional[WorkOrder]:
	with get_db_context(client_id=client_id) as session:
		row = session.execute(
			text(
				f"SELECT {_COLUMNS_SQL} FROM agent_work_orders "
				"WHERE client_id = :client_id AND idempotency_key = :idempotency_key"
			),
			{"client_id": client_id, "idempotency_key": idempotency_key},
		).mappings().first()
		return _row_to_order(dict(row)) if row else None


def set_slack_message(client_id: str, action_id: str, *, channel_id: str, message_ts: str) -> None:
	"""Stores BOTH channel_id and ts — FA stored only ts and had to
	re-resolve the channel at edit time (FA/admin_router.py:1822-1856
	documents that a ts from one channel cannot be edited in another);
	storing both here removes that failure mode entirely."""
	with get_db_context(client_id=client_id) as session:
		session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET slack_channel_id = :channel_id, slack_message_ts = :message_ts, updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id"
			),
			{"channel_id": channel_id, "message_ts": message_ts, "action_id": action_id, "client_id": client_id},
		)


_TERMINAL_DECISIONS = {"APPROVED", "REJECTED", "SNOOZED", "SKIPPED", "DONE"}


def record_decision(client_id: str, action_id: str, *, decision: str, decided_by: str) -> Optional[WorkOrder]:
	"""Flips a QUEUED row to a terminal decision. Returns None (no-op) if
	the row was not QUEUED at the moment of update — guards a double
	button-press or a stale/duplicate Slack retry from re-deciding an
	already-decided row. Direct fork of the guard shape in
	FA/relay/queue.py:record_decision (WHERE ... AND status = pending)."""
	if decision not in _TERMINAL_DECISIONS:
		raise ValueError(f"record_decision: {decision!r} is not a terminal decision")

	with get_db_context(client_id=client_id) as session:
		result = session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET status = :decision, decided_by = :decided_by, decided_at = NOW(), updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
			),
			{"decision": decision, "decided_by": decided_by, "action_id": action_id, "client_id": client_id},
		)
		if result.rowcount == 0:
			return None
	return get(client_id, action_id)


def snooze(client_id: str, action_id: str, *, until: datetime, decided_by: str) -> Optional[WorkOrder]:
	with get_db_context(client_id=client_id) as session:
		result = session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET status = 'SNOOZED', due_at = :until, decided_by = :decided_by, "
				"    decided_at = NOW(), updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
			),
			{"until": until, "decided_by": decided_by, "action_id": action_id, "client_id": client_id},
		)
		if result.rowcount == 0:
			return None
	return get(client_id, action_id)


def update_payload(client_id: str, action_id: str, *, payload: dict, config_fingerprint: dict) -> Optional[WorkOrder]:
	"""The ONLY sanctioned way to mutate a QUEUED order's content —
	recomputes and persists payload_hash in the SAME transaction, so no
	caller can leave a stale hash behind. Guarded WHERE status = 'QUEUED':
	a decided order is immutable. Returns None if the row was not QUEUED."""
	current = get(client_id, action_id)
	if current is None:
		return None

	digest = _hash.compute(
		_HashInput(
			client_id=current.client_id,
			action_id=current.action_id,
			action_class=current.action_class,
			entity_type=current.entity_type,
			entity_id=current.entity_id,
			recipient=current.recipient,
			payload=payload,
			config_fingerprint=config_fingerprint,
		)
	)  # type: ignore[arg-type]

	with get_db_context(client_id=client_id) as session:
		result = session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET payload = :payload, config_fingerprint = :config_fingerprint, "
				"    payload_hash = :payload_hash, updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'QUEUED'"
			),
			{
				"payload": json.dumps(payload),
				"config_fingerprint": json.dumps(config_fingerprint),
				"payload_hash": digest,
				"action_id": action_id,
				"client_id": client_id,
			},
		)
		if result.rowcount == 0:
			return None
	return get(client_id, action_id)


def queued_depth(client_id: Optional[str] = None, *, older_than: Optional[timedelta] = None) -> int:
	"""What Dev 2 §C's Cora throttle reads (50-count and 24h-age
	triggers). client_id=None aggregates across all tenants via
	get_system_db_context() — batch/throttle use only, never src/api/."""
	where = ["status = 'QUEUED'"]
	params: dict = {}
	if client_id is not None:
		where.append("client_id = :client_id")
		params["client_id"] = client_id
	if older_than is not None:
		where.append("created_at <= :cutoff")
		params["cutoff"] = datetime.now(timezone.utc) - older_than

	sql = f"SELECT COUNT(*) FROM agent_work_orders WHERE {' AND '.join(where)}"

	if client_id is not None:
		with get_db_context(client_id=client_id) as session:
			return session.execute(text(sql), params).scalar() or 0
	with get_system_db_context() as session:
		return session.execute(text(sql), params).scalar() or 0


def approved_batch(client_id: Optional[str] = None, *, limit: int = 50) -> list:
	"""APPROVED rows ready for dispatch, oldest first — what --sweep hands
	to a registered dispatcher (src.services.work_orders.dispatchers).
	client_id=None aggregates across all tenants via
	get_system_db_context() — same batch-only rule as queued_depth."""
	where = ["status = 'APPROVED'"]
	params: dict = {"limit": limit}
	if client_id is not None:
		where.append("client_id = :client_id")
		params["client_id"] = client_id

	sql = f"SELECT {_COLUMNS_SQL} FROM agent_work_orders WHERE {' AND '.join(where)} ORDER BY created_at ASC LIMIT :limit"

	if client_id is not None:
		with get_db_context(client_id=client_id) as session:
			rows = session.execute(text(sql), params).mappings().all()
	else:
		with get_system_db_context() as session:
			rows = session.execute(text(sql), params).mappings().all()
	return [_row_to_order(dict(r)) for r in rows]


def requeue_due_snoozed(client_id: str) -> list:
	"""SNOOZED rows whose due_at has passed -> QUEUED, so a snooze actually
	expires. Returns the revived orders (callers re-post their cards).

	Without this, snooze was a one-way trip: listeners.handle_snooze set
	status='SNOOZED' + due_at, but nothing ever read due_at back —
	approved_batch() only selects APPROVED — so "Snooze 1h" silently
	deleted the order from the workflow forever.

	One UPDATE ... RETURNING, not SELECT-then-UPDATE: the status='SNOOZED'
	guard inside the same statement is what makes a due order revive
	EXACTLY once even if two sweeps race. decided_by/decided_at are cleared
	because the row is genuinely undecided again — _load_and_verify's
	status == 'QUEUED' check is what re-opens the card's buttons. due_at is
	deliberately KEPT as the record of what the snooze was set to."""
	with get_db_context(client_id=client_id) as session:
		rows = session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET status = 'QUEUED', decided_by = NULL, decided_at = NULL, updated_at = NOW() "
				"WHERE client_id = :client_id AND status = 'SNOOZED' "
				"  AND due_at IS NOT NULL AND due_at <= NOW() "
				"RETURNING action_id"
			),
			{"client_id": client_id},
		).mappings().all()
		revived_ids = [str(r["action_id"]) for r in rows]
	return [o for o in (get(client_id, aid) for aid in revived_ids) if o is not None]


# How long an EXECUTING row may sit untouched before a sweep assumes the
# worker that claimed it died and hands it back. Generous on purpose: the
# cost of reclaiming too early (a double dispatch) is worse than the cost
# of reclaiming too late (a delayed send).
STALE_EXECUTING_AFTER = timedelta(minutes=30)


def reclaim_stale_executing(client_id: str, *, older_than: timedelta = STALE_EXECUTING_AFTER) -> list:
	"""EXECUTING rows untouched for longer than `older_than` -> APPROVED,
	so the next sweep re-dispatches them. Returns the reclaimed action_ids.

	claim_for_execution() commits APPROVED -> EXECUTING BEFORE the
	dispatcher runs (see __main__.cmd_sweep). If that process is killed
	between the claim and record_execution_result(), the row stays
	EXECUTING and no later sweep will ever look at it again — approved_batch
	selects only APPROVED. This is the reclaim path that makes the claim
	survivable; it is a crash-recovery concern, distinct from (and present
	even without) the concurrency concern noted on claim_for_execution.

	ponytail: wall-clock timeout, no lease/heartbeat — a dispatch that
	legitimately runs longer than `older_than` would be reclaimed and
	double-executed. Fine while Week 0 dispatchers are fast and
	non-concurrent; move to a lease renewed by the running worker (or an
	outbox with idempotent retry) once a dispatcher can run long."""
	cutoff = datetime.now(timezone.utc) - older_than
	with get_db_context(client_id=client_id) as session:
		rows = session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET status = 'APPROVED', updated_at = NOW() "
				"WHERE client_id = :client_id AND status = 'EXECUTING' AND updated_at <= :cutoff "
				"RETURNING action_id"
			),
			{"client_id": client_id, "cutoff": cutoff},
		).mappings().all()
		return [str(r["action_id"]) for r in rows]


def claim_for_execution(client_id: str, action_id: str) -> Optional[WorkOrder]:
	"""Atomically transitions APPROVED -> EXECUTING. Returns None if the
	row was not APPROVED at the moment of claim — guards two concurrent
	--sweep runs from both claiming and dispatching the same order.

	ponytail: no batch_id tracking (FA/relay/queue.py's try_claim_for_batch
	has one) — Week 0 runs --sweep as a single, non-concurrent process, so
	attributing a claim to a specific worker buys nothing yet. Add batch_id
	if --sweep ever runs as more than one concurrent worker. A crashed
	claim is recovered by reclaim_stale_executing() above, which needs no
	batch_id."""
	with get_db_context(client_id=client_id) as session:
		result = session.execute(
			text(
				"UPDATE agent_work_orders SET status = 'EXECUTING', updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'APPROVED'"
			),
			{"action_id": action_id, "client_id": client_id},
		)
		if result.rowcount == 0:
			return None
	return get(client_id, action_id)


def record_execution_result(
	client_id: str, action_id: str, *, success: bool, receipt: dict, error: Optional[str] = None
) -> Optional[WorkOrder]:
	"""Transitions a claimed (EXECUTING) row to its terminal outcome —
	DONE on success, FAILED otherwise. Guarded WHERE status = 'EXECUTING':
	only a row this same claim path actually claimed can be finalized."""
	new_status = "DONE" if success else "FAILED"
	with get_db_context(client_id=client_id) as session:
		result = session.execute(
			text(
				"UPDATE agent_work_orders "
				"SET status = :new_status, execution_receipt = :receipt, error = :error, updated_at = NOW() "
				"WHERE action_id = :action_id AND client_id = :client_id AND status = 'EXECUTING'"
			),
			{
				"new_status": new_status,
				"receipt": json.dumps(receipt),
				"error": error,
				"action_id": action_id,
				"client_id": client_id,
			},
		)
		if result.rowcount == 0:
			return None
	return get(client_id, action_id)
