"""Scheduled confirmation-delivery worker (Subtask 3.2.1) — retries
PENDING/FAILED-and-due/claim-expired confirmation jobs on a short
interval. Uses the identical atomic claim-and-send code path as the
webhook route's immediate best-effort attempt
(src/services/calendar_confirmation.py), so there is exactly one
implementation of "how a confirmation gets sent," not two that could
drift apart.

Runs under the BYPASSRLS system session — this sweep spans every
client's bookings by nature, same posture as
county_allocation_reassessment.py and calendar_sync_worker.py.
"""

import logging

from src.core.database import get_system_db_context
from src.services.calendar_confirmation import claim_confirmations, send_confirmation_for_booking

logger = logging.getLogger(__name__)


def run_sweep(limit: int = 20) -> int:
	sent = 0
	with get_system_db_context() as session:
		claimed = claim_confirmations(session, limit=limit)
		for booking_id in claimed:
			send_confirmation_for_booking(session, booking_id)
			sent += 1
	logger.info("booking_confirmation_sender: processed %d confirmation job(s)", sent)
	return sent


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sweep()
