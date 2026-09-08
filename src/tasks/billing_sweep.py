"""Six Billing Rules sweeps (Subtask 1.2.3).

Three independent sweeps, all under get_system_db_context() (BYPASSRLS —
billing rules span every tenant), each claim_time/as_of explicit so the
60-second ack and 60-day guarantee clocks are fast-forwardable in tests
without clock mocking. Per-row session.begin_nested() so one bad row can't
discard the batch, same pattern as src/tasks/settlement_sweep.py and
src/tasks/self_serve_audit_worker.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.billing.dispute_credit import DisputeWindowExpiredError, credit_dispute_on_flag
from src.services.billing.guarantee import evaluate_sixty_day_guarantee
from src.services.billing.miss_credit import claim_missed_acks, process_missed_ack

logger = logging.getLogger(__name__)


def run_miss_credit_sweep(limit: int = 100, *, claim_time: datetime | None = None) -> int:
	claim_time = claim_time or datetime.now(timezone.utc)
	credited = 0
	claimed_count = 0
	with get_system_db_context() as session:
		claimed = claim_missed_acks(session, claim_time=claim_time, limit=limit)
		claimed_count = len(claimed)
		for message in claimed:
			with session.begin_nested():
				if process_missed_ack(session, message, as_of=claim_time):
					credited += 1
	logger.info("billing_sweep.miss_credit: processed %d message(s), credited %d", claimed_count, credited)
	return credited


def run_dispute_credit_sweep(limit: int = 100, *, claim_time: datetime | None = None) -> int:
	"""Claims appointment_disputes rows still in credit_status='PENDING' —
	credit_dispute_on_flag() itself flips the row to CREDITED or EXPIRED
	before returning/raising, so a row is claimed by this query AT MOST until
	its first (successful or window-expired) evaluation, never forever. An
	earlier version of this query LEFT JOINed billing_credits instead, which
	correctly prevented a double credit (billing_credits' own UNIQUE
	constraint is still that guarantee) but never excluded an EXPIRED
	dispute from being re-selected — every sweep tick reprocessed it, raised
	DisputeWindowExpiredError again, and logged the same warning forever.

	Deliberately NOT wrapped in a per-row session.begin_nested() (unlike the
	other two sweeps in this module) — DisputeWindowExpiredError is the only
	exception this loop anticipates, and credit_dispute_on_flag's own
	credit_status='EXPIRED' UPDATE must survive that exception. A savepoint
	that is rolled back on exception exit would undo that write along with
	everything else since the savepoint, no matter how it's nested inside
	credit_dispute_on_flag itself — the write can only survive by not being
	inside a savepoint that gets rolled back at all."""
	claim_time = claim_time or datetime.now(timezone.utc)
	credited = 0
	with get_system_db_context() as session:
		rows = session.execute(
			text(
				"SELECT dispute_id FROM appointment_disputes "
				"WHERE outcome = 'CREDITED_AUTOMATIC' AND credit_status = 'PENDING' "
				"ORDER BY flagged_at LIMIT :limit"
			),
			{"limit": limit},
		).all()
		for row in rows:
			try:
				if credit_dispute_on_flag(session, dispute_id=str(row.dispute_id), as_of=claim_time):
					credited += 1
			except DisputeWindowExpiredError:
				logger.warning("billing_sweep.dispute_credit: dispute %s outside 48h window — not credited", row.dispute_id)
	logger.info("billing_sweep.dispute_credit: credited %d dispute(s)", credited)
	return credited


def run_guarantee_sweep(limit: int = 100, *, claim_time: datetime | None = None) -> int:
	claim_time = claim_time or datetime.now(timezone.utc)
	applied = 0
	with get_system_db_context() as session:
		rows = session.execute(
			text(
				"SELECT DISTINCT ce.client_id FROM client_entitlements ce "
				"WHERE ce.status = 'ACTIVE' AND ce.guarantee_applied = FALSE "
				"  AND ce.offer_code IN ('owner_growth', 'full_county') "
				"LIMIT :limit"
			),
			{"limit": limit},
		).all()
		for row in rows:
			with session.begin_nested():
				if evaluate_sixty_day_guarantee(session, client_id=row.client_id, as_of=claim_time):
					applied += 1
	logger.info("billing_sweep.guarantee: applied %d override(s)", applied)
	return applied


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_miss_credit_sweep()
	run_dispute_credit_sweep()
	run_guarantee_sweep()
