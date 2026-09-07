"""Scheduled no-show recovery email worker (Subtask 3.2.3) — drains
PENDING/FAILED-and-due/claim-expired no_show_recovery_jobs on a short
interval. The Slack mark_no_show click handler only ever enqueues a
PENDING row (src/services/no_show_recovery.py); this is what actually
sends, comfortably inside the DoD's 5-minute budget when run on an
interval well under it (mirrors
_CONFIRMATION_SWEEP_INTERVAL_SECONDS's 30s in src/api/main.py).

Runs under the BYPASSRLS system session — spans every client's recovery
jobs by nature, same posture as show_rate_reminder_sender.py.
"""

import logging

from src.core.database import get_system_db_context
from src.services.no_show_recovery_dispatch import claim_recovery_jobs, send_recovery_email

logger = logging.getLogger(__name__)


def run_sweep(limit: int = 20) -> int:
	from datetime import datetime, timezone

	sent = 0
	with get_system_db_context() as session:
		claimed = claim_recovery_jobs(session, claim_time=datetime.now(timezone.utc), limit=limit)
		for recovery_job_id in claimed:
			send_recovery_email(session, recovery_job_id)
			sent += 1
	logger.info("no_show_recovery_sender: processed %d recovery job(s)", sent)
	return sent


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sweep()
