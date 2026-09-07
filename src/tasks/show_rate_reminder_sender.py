"""Scheduled reminder-delivery worker (Subtask 3.2.2) — drains
PENDING/FAILED-and-due/claim-expired booking_reminder_jobs on a short
interval, using the identical atomic claim-and-send code path
(src/services/show_rate_reminders.py) regardless of caller.

`as_of` is a real parameter, not read from SQL NOW() — see
show_rate_reminders.claim_reminders()'s docstring for why: it's what
makes the fast-forward timing test possible (pass a fixed instant at
`meeting_start - 24h` / `- 30m` instead of waiting real wall-clock time).

Runs under the BYPASSRLS system session — this sweep spans every
client's reminder jobs by nature, same posture as
booking_confirmation_sender.py.
"""

import logging
from datetime import datetime, timezone

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.show_rate_reminders import claim_reminders, recover_blocked_jobs, send_show_rate_reminder

logger = logging.getLogger(__name__)


def run_sweep(limit: int = 20, *, as_of: datetime = None) -> int:
	as_of = as_of or datetime.now(timezone.utc)
	sent = 0
	with get_system_db_context() as session:
		recover_blocked_jobs(session, email_sending_enabled=get_settings().email_sending_enabled)
		claimed = claim_reminders(session, claim_time=as_of, limit=limit)
		for reminder_job_id in claimed:
			send_show_rate_reminder(session, reminder_job_id, as_of=as_of)
			sent += 1
	logger.info("show_rate_reminder_sender: processed %d reminder job(s)", sent)
	return sent


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sweep()
