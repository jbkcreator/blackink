"""Rule 1 — $50 miss credit (Subtask 1.2.3).

"Any Respond inbound email not acknowledged inside 60 seconds automatically
writes a $50 credit line to the client's next invoice. No human approval
required." (docs/Sept04_New_Items_Triage.md item — "$50 Miss Credit")

acked_at / ack_latency_seconds are new columns
(migrations/apply_entitlements_billing.py) — acked_at is the AUTOMATED
first-response timestamp, deliberately distinct from inbound_messages.claimed_at
(the 30-minute human-SLA claim clock). Nothing in this module writes acked_at;
that is the responsibility of whatever sends the automated first response
(a separate, not-yet-built code path — the Reply Triage Agent worker is the
likely home, out of scope for this ledger/credit change).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.billing.credits import issue_credit
from src.services.events import log_event

logger = logging.getLogger(__name__)

_MISS_CREDIT_AMOUNT_CENTS = 5000
_ACK_THRESHOLD_SECONDS = 60
_CLAIM_LIMIT = 100


def claim_missed_acks(session: Session, *, claim_time: datetime, limit: int = _CLAIM_LIMIT) -> list[dict]:
	"""SKIP LOCKED claim of inbound_messages rows past the 60-second ack
	threshold and not yet classified/credited. Returns the claimed rows'
	(id, client_id, ack_latency_seconds, channel) — the caller issues the
	credit and stamps miss_credit_issued_at in the SAME transaction (per-row
	begin_nested(), same pattern as every other sweep in this repo).

	Two distinct miss shapes, both claimed here: a message acked LATE
	(ack_latency_seconds > 60, the generated column already reflects the real
	gap) and a message NEVER acked at all (acked_at still NULL — the
	generated column is NULL too and can never satisfy "> 60" on its own,
	which would otherwise make the worst-case SLA breach invisible to this
	sweep forever). For the never-acked branch, ack_latency_seconds is
	computed here as the gap from received_at to claim_time (an as-of-now
	estimate, since there is no real ack yet)."""
	rows = session.execute(
		text(
			f"""
			SELECT id, client_id, channel,
				COALESCE(ack_latency_seconds, EXTRACT(EPOCH FROM (:claim_time - received_at))::INTEGER) AS ack_latency_seconds
			FROM inbound_messages
			WHERE channel = 'EMAIL'
				AND intent IS NULL
				AND miss_credit_issued_at IS NULL
				AND (
					ack_latency_seconds > {_ACK_THRESHOLD_SECONDS}
					OR (acked_at IS NULL AND received_at <= :claim_time - INTERVAL '{_ACK_THRESHOLD_SECONDS} seconds')
				)
			ORDER BY id
			FOR UPDATE SKIP LOCKED
			LIMIT :limit
			"""
		),
		{"limit": limit, "claim_time": claim_time},
	).all()
	return [
		{"id": r.id, "client_id": r.client_id, "ack_latency_seconds": r.ack_latency_seconds, "channel": r.channel}
		for r in rows
	]


def process_missed_ack(session: Session, message: dict, *, as_of: datetime) -> bool:
	"""Issues the $50 credit for one already-claimed inbound_messages row and
	stamps miss_credit_issued_at. Returns whether a NEW credit was written
	(False on a duplicate — see issue_credit()). Logs respond_ack_missed
	unconditionally (the miss itself happened regardless of credit dedup);
	billing_credit_issued is logged by issue_credit() only on a genuine
	write — together these satisfy "both the miss event and the credit are
	visible on the proof ledger" (interpreted as the events stream; see
	migrations/apply_entitlements_billing.py)."""
	billing_period = date(as_of.year, as_of.month, 1)

	log_event(
		message["client_id"], "respond_ack_missed", entity_type="inbound_message", entity_id=str(message["id"]),
		payload={
			"inbound_message_id": message["id"],
			"ack_latency_seconds": message["ack_latency_seconds"],
			"channel": message["channel"],
		},
		session=session,
	)

	credited = issue_credit(
		session,
		client_id=message["client_id"],
		credit_type="MISS_CREDIT",
		amount_cents=_MISS_CREDIT_AMOUNT_CENTS,
		source_table="inbound_messages",
		source_id=str(message["id"]),
		issued_at=as_of,
		billing_period=billing_period,
	)

	session.execute(
		text("UPDATE inbound_messages SET miss_credit_issued_at = :as_of WHERE id = :id"),
		{"as_of": as_of, "id": message["id"]},
	)

	if credited:
		logger.info(
			"billing.miss_credit: issued $50 credit (client=%s inbound_message=%s ack_latency=%ds)",
			message["client_id"], message["id"], message["ack_latency_seconds"],
		)
	return credited
