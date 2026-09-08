"""The single write path into billing_credits (Subtask 1.2.3, rules 1 and 4).

Same "one write path" posture as src/services/events.py's log_event() and
src/services/settlement/ledger.py's mark_installment(): every credit —
whatever rule produced it — goes through issue_credit(), so the
UNIQUE(client_id, credit_type, source_table, source_id) guarantee (no double
credit for the same miss/dispute) is enforced in exactly one place.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.services.events import log_event

logger = logging.getLogger(__name__)


def issue_credit(
	session: Session,
	*,
	client_id: str,
	credit_type: str,
	amount_cents: int,
	source_table: str,
	source_id: str,
	issued_at: datetime,
	billing_period: date,
) -> bool:
	"""Inserts one billing_credits row and logs billing_credit_issued in the
	SAME transaction. Returns False (and logs, no exception) on a duplicate
	(client_id, credit_type, source_table, source_id) — a caller retrying the
	same miss/dispute must never write a second credit line."""
	try:
		with session.begin_nested():
			row = session.execute(
				text(
					"INSERT INTO billing_credits "
					"(client_id, credit_type, amount_cents, source_table, source_id, issued_at, billing_period) "
					"VALUES (:client_id, :credit_type, :amount_cents, :source_table, :source_id, :issued_at, :billing_period) "
					"RETURNING credit_id"
				),
				{
					"client_id": client_id,
					"credit_type": credit_type,
					"amount_cents": amount_cents,
					"source_table": source_table,
					"source_id": str(source_id),
					"issued_at": issued_at,
					"billing_period": billing_period,
				},
			).one()
	except IntegrityError:
		# SAVEPOINT rollback via begin_nested(), not session.rollback() — see
		# settlement/ledger.py's record_door_signed() for why a bare
		# session.rollback() here would discard the caller's whole transaction.
		logger.info(
			"billing.issue_credit: duplicate (client=%s type=%s source=%s:%s) — no-op",
			client_id, credit_type, source_table, source_id,
		)
		return False

	log_event(
		client_id, "billing_credit_issued", entity_type="billing_credit", entity_id=str(row.credit_id),
		payload={
			"credit_id": row.credit_id, "credit_type": credit_type,
			"amount_cents": amount_cents, "billing_period": billing_period.isoformat(),
		},
		session=session,
	)
	return True
