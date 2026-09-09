"""Settlement engine sweeps (Subtask 1.2.2).

Three independent sweeps, all under get_system_db_context() (BYPASSRLS —
settlements span every tenant), each claim_time explicit and threaded down
as as_of so the 60-day clock is fast-forwardable in tests without clock
mocking (see tests/test_settlement_clawback_rules.py). Per-row
session.begin_nested() so one bad row can't discard the batch's attempts
increments, same pattern as self_serve_audit_worker.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.pms_sync import StubPmsProvider
from src.services.settlement.charge import charge_installment
from src.services.settlement.clawback import process_installment_2
from src.services.settlement.ledger import claim_installment_1, claim_installment_2

logger = logging.getLogger(__name__)


def run_door_signed_sweep(limit: int = 20, *, claim_time: datetime | None = None) -> int:
	"""Polls the (stubbed) PmsProvider for newly-confirmed door_signed
	agreements and opens a settlement for each. With StubPmsProvider (the
	only implementation today) this always returns 0 — no PMS integration
	is contracted, so nothing opens from a real sync."""
	from src.services.settlement.ledger import open_settlement, record_door_signed

	pms = StubPmsProvider()
	opened = 0
	with get_system_db_context() as session:
		clients = session.execute(text("SELECT client_id FROM clients WHERE is_active")).all()
		for row in clients:
			agreements = pms.fetch_new_agreements(row.client_id)
			if not agreements:
				continue
			for agreement in agreements:
				with session.begin_nested():
					pms_agreement_id = record_door_signed(
						session, client_id=row.client_id, opportunity_id=agreement.external_ref,
						door_signed_at=agreement.door_signed_at, agreement_source="PMS_SYNC",
						pms_property_ref=agreement.pms_property_ref, door_count=agreement.door_count,
						owner_domain=agreement.owner_domain, owner_email=agreement.owner_email,
					)
					if pms_agreement_id is not None:
						opened += 1
	logger.info("settlement_sweep.door_signed: opened %d settlement(s)", opened)
	return opened


def run_installment_1_sweep(limit: int = 20, *, claim_time: datetime | None = None) -> int:
	claim_time = claim_time or datetime.now(timezone.utc)
	charged = 0
	with get_system_db_context() as session:
		claimed = claim_installment_1(session, claim_time=claim_time, limit=limit)
		for transaction_id in claimed:
			with session.begin_nested():
				charge_installment(session, transaction_id, 1, as_of=claim_time)
			charged += 1
	logger.info("settlement_sweep.installment_1: processed %d transaction(s)", charged)
	return charged


def run_installment_2_sweep(limit: int = 20, *, claim_time: datetime | None = None) -> int:
	claim_time = claim_time or datetime.now(timezone.utc)
	pms = StubPmsProvider()
	processed = 0
	with get_system_db_context() as session:
		claimed = claim_installment_2(session, claim_time=claim_time, limit=limit)
		for transaction_id in claimed:
			with session.begin_nested():
				process_installment_2(session, transaction_id, as_of=claim_time, pms=pms)
			processed += 1
	logger.info("settlement_sweep.installment_2: processed %d transaction(s)", processed)
	return processed


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_door_signed_sweep()
	run_installment_1_sweep()
	run_installment_2_sweep()
