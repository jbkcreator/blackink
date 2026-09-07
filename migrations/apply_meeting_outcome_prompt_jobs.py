"""
Provision meeting_outcome_prompt_jobs (Addendum to Subtask 3.2.1 —
Post-Booking "Log Outcome" Trigger Card).

One row per INTERNAL_SALES_DEMO booking, fired at exactly
bookings.scheduled_at (the meeting's own start time — never a guessed end
time) by src/tasks/meeting_outcome_prompt_sender.py, which posts a "Log
Outcome" card to #blackink-setter. Sibling to no_show_prompt_jobs (see
apply_no_show_prompt_jobs.py) — same SKIP LOCKED claim shape, its own table
because the outcome vocabulary and the follow-up lifecycle differ.

Scoped to INTERNAL_SALES_DEMO only: meeting_outcomes.contact_id/company_id
are NOT NULL FKs to contacts/companies, and only a sales-demo booking
carries target_contact_id/target_company_id. A CLIENT_OWNER_BOOKING links to
owner_contacts and has no contacts row at all, so a "closer logs a demo
outcome" row is unrepresentable for one.

The card itself is a real agent_work_orders row (work_order_action_id below),
so its SHA-256 payload/recipient/config binding comes from the existing
src/services/slack/payload_hash.py rather than a second implementation. The
24-hour card expiry is NOT part of that hash (adding a timestamp window to
the FIXED preimage would change every digest and force a HASH_VERSION bump) —
it is an explicit created_at + 24h check at click and submit time.

Status lifecycle: PENDING -> SENDING -> SENT, with:
  SKIPPED   - scheduled_for already past when the job was created, or an
              outcome for this booking was already recorded elsewhere (3.2.3's
              "Mark No-Show" card in #blackink-command fired first).
  BLOCKED   - a required input is missing, so the card cannot be posted
              attributably: the booking's target_contact_id/target_company_id
              is unresolved (PENDING_RECONCILIATION), or the rep's
              calendar_connections row has no rep_slack_user_id. Both are
              re-checked and auto-promoted back to PENDING by the sweep's
              self-heal step once the underlying data appears — the same
              pattern src/services/show_rate_reminders.py uses for
              MISSING_OVS_*.
  CANCELLED - the booking itself was cancelled before this job fired.
  EXPIRED   - the card's own 24-hour window closed with no outcome logged.
              No further reminder pings, matching how every other expired
              interactive card in the system behaves.

posted_at anchors both the 4-hour unclicked reminder ping and the 24-hour
expiry; reminder_sent_at makes that ping exactly-once.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_meeting_outcome_prompt_jobs.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS meeting_outcome_prompt_jobs (
		prompt_job_id          BIGSERIAL    PRIMARY KEY,
		client_id              VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		booking_id             BIGINT       NOT NULL REFERENCES bookings(booking_id),
		scheduled_for          TIMESTAMPTZ  NOT NULL,
		status                 VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
		last_error             TEXT,
		work_order_action_id   UUID,
		slack_channel_id       VARCHAR(20),
		slack_message_ts       VARCHAR(30),
		posted_at              TIMESTAMPTZ,
		reminder_sent_at       TIMESTAMPTZ,
		claimed_at             TIMESTAMPTZ,
		created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_meeting_outcome_prompt_jobs_booking UNIQUE (booking_id),
		CONSTRAINT ck_meeting_outcome_prompt_jobs_status CHECK (
			status IN ('PENDING', 'SENDING', 'SENT', 'SKIPPED', 'BLOCKED',
			           'CANCELLED', 'FAILED', 'EXPIRED')
		)
	)
	""",
	# Retry accounting for transient Slack post failures — a temporary Slack
	# outage at meeting time must NOT permanently strand the card. post_prompt()
	# re-queues a failed post with a bounded exponential backoff (attempts /
	# next_retry_at) rather than marking it terminally FAILED on the first miss.
	"ALTER TABLE meeting_outcome_prompt_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
	"ALTER TABLE meeting_outcome_prompt_jobs ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ",

	# NOTE: the meeting_outcomes duplicate-outcome backstop is NOT added here.
	# The canonical meeting_outcomes table (apply_meeting_outcomes.py) already
	# carries UNIQUE (client_id, contact_id, meeting_occurred_at), and
	# record_outcome() upserts through it — a contact belongs to exactly one
	# client, so that constraint already enforces one outcome per meeting.

	"CREATE INDEX IF NOT EXISTS ix_meeting_outcome_prompt_jobs_booking ON meeting_outcome_prompt_jobs (booking_id)",
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcome_prompt_jobs_status ON meeting_outcome_prompt_jobs (status)",
	# Supports the 4h-ping / 24h-expiry sweep over already-posted cards.
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcome_prompt_jobs_posted ON meeting_outcome_prompt_jobs (status, posted_at)",
	# Same least-privilege posture as no_show_prompt_jobs — no DELETE for
	# either role, a job row is never removed, only status-transitioned.
	"REVOKE DELETE ON meeting_outcome_prompt_jobs FROM blackink_app",
	"REVOKE DELETE ON meeting_outcome_prompt_jobs FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON meeting_outcome_prompt_jobs TO blackink_app",
	"GRANT USAGE ON SEQUENCE meeting_outcome_prompt_jobs_prompt_job_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON meeting_outcome_prompt_jobs TO blackink_system",
	"GRANT USAGE ON SEQUENCE meeting_outcome_prompt_jobs_prompt_job_id_seq TO blackink_system",

	# The prompt sweep (src/tasks/meeting_outcome_prompt_sender.py, runs as
	# blackink_system) reads meeting_outcomes via outcome_recorded_for_booking()
	# to decide whether a card still needs posting / pinging. The canonical
	# meeting_outcomes migration grants SELECT only to blackink_app, so this
	# addendum — the first blackink_system reader of that table — adds the
	# read grant it needs. Read-only: the sweep never writes meeting_outcomes.
	"GRANT SELECT ON meeting_outcomes TO blackink_system",
]



def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_meeting_outcome_prompt_jobs: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
