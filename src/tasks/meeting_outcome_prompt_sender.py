"""Scheduled "Log Outcome" trigger-card worker (Addendum to Subtask
3.2.1) — posts one card per INTERNAL_SALES_DEMO booking at its
scheduled_at, then handles that card's 4-hour unclicked ping and 24-hour
expiry.

Four steps per tick, in this order:
  1. self-heal BLOCKED rows whose missing input has since arrived (an
     unresolved target reconciled, or rep_slack_user_id provisioned),
  2. claim due PENDING / claim-expired SENDING rows,
  3. post each claimed card,
  4. sweep already-posted cards for the reminder ping and the expiry.

Step 1 runs before step 2 deliberately: a row healed this tick is eligible
to post on the same tick rather than waiting a full interval.

`as_of` is a real parameter, not SQL NOW() — same rationale as
no_show_prompt_sender.py: it's what makes fast-forward tests of the 4-hour
and 24-hour windows possible without a real wait.

Runs under the BYPASSRLS system session: it spans every client's jobs, and
the prospect name on the card lives in contacts/companies rows that are
RLS-invisible to a session scoped to the internal-sales client (their
companies.owning_client_id is NULL).
"""

import asyncio
import logging
from datetime import datetime, timezone

from src.core.database import get_system_db_context
from src.services.meeting_outcome_prompts import (
	claim_prompts,
	post_prompt,
	self_heal_blocked,
	sweep_posted_cards,
)

logger = logging.getLogger(__name__)


def run_sweep(limit: int = 20, *, as_of: datetime = None) -> int:
	as_of = as_of or datetime.now(timezone.utc)
	posted = 0
	with get_system_db_context() as session:
		healed = self_heal_blocked(session)
		claimed = claim_prompts(session, claim_time=as_of, limit=limit)
		for prompt_job_id in claimed:
			asyncio.run(post_prompt(session, prompt_job_id, as_of=as_of))
			posted += 1
		followups = asyncio.run(sweep_posted_cards(session, as_of=as_of))
	logger.info(
		"meeting_outcome_prompt_sender: healed=%d posted=%d pinged=%d expired=%d",
		healed, posted, followups["pinged"], followups["expired"],
	)
	return posted


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_sweep()
