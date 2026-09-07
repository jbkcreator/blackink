"""Scheduled "Mark No-Show" prompt worker (Subtask 3.2.3) — drains
PENDING/claim-expired no_show_prompt_jobs on a short interval, posting
one Slack card per INTERNAL_SALES_DEMO booking at its scheduled_at.

`as_of` is a real parameter, not read from SQL NOW() — same rationale
as show_rate_reminder_sender.py: it's what makes a fast-forward test of
the 10-minute timing window possible.

Runs under the BYPASSRLS system session — spans every client's prompt
jobs by nature, same posture as show_rate_reminder_sender.py.
"""

import asyncio
import logging
from datetime import datetime, timezone

from src.core.database import get_system_db_context
from src.services.no_show_prompts import claim_prompts, send_no_show_prompt

logger = logging.getLogger(__name__)


def run_sweep(limit: int = 20, *, as_of: datetime = None) -> int:
	as_of = as_of or datetime.now(timezone.utc)
	sent = 0
	with get_system_db_context() as session:
		claimed = claim_prompts(session, claim_time=as_of, limit=limit)
		for prompt_job_id in claimed:
			asyncio.run(send_no_show_prompt(session, prompt_job_id, as_of=as_of))
			sent += 1
	logger.info("no_show_prompt_sender: processed %d prompt job(s)", sent)
	return sent


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sweep()
