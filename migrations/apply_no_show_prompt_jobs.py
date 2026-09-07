"""
Provision no_show_prompt_jobs (Subtask 3.2.3 — No-Show Handler).

One row per INTERNAL_SALES_DEMO booking, fired at exactly
bookings.scheduled_at (never earlier) by src/tasks/no_show_prompt_sender.py,
which posts a "Mark No-Show" Slack card. Mirrors booking_reminder_jobs'
SKIP LOCKED claim shape (see apply_booking_reminder_jobs.py) but is its
own table — this job posts a Slack card, not an email, and its outcome
vocabulary is different.

Status lifecycle: PENDING -> SENDING -> SENT, with:
  SKIPPED   - claimed too late to be useful (as_of > scheduled_for + 10min)
              — a prompt posted after the DoD's own 10-minute window has
              already closed would invite an incorrect late click.
  BLOCKED   - the booking's target_contact_id is unresolved
              (PENDING_RECONCILIATION — booking_ingest.py could not match
              the webhook's captured email to a contact). Never show an
              actionable "Mark No-Show" button for an inferred/unknown
              contact — this stays BLOCKED for manual reconciliation
              instead of silently posting a button that would pause the
              wrong contact if clicked.
  CANCELLED - the booking itself was cancelled before this job fired.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_no_show_prompt_jobs.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS no_show_prompt_jobs (
		prompt_job_id      BIGSERIAL    PRIMARY KEY,
		client_id          VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		booking_id         BIGINT       NOT NULL REFERENCES bookings(booking_id),
		scheduled_for       TIMESTAMPTZ  NOT NULL,
		status                 VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
		last_error                 TEXT,
		slack_channel_id           VARCHAR(20),
		slack_message_ts               VARCHAR(30),
		claimed_at                         TIMESTAMPTZ,
		created_at                             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at                                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_no_show_prompt_jobs_booking UNIQUE (booking_id),
		CONSTRAINT ck_no_show_prompt_jobs_status CHECK (
			status IN ('PENDING', 'SENDING', 'SENT', 'SKIPPED', 'BLOCKED', 'CANCELLED', 'FAILED')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_no_show_prompt_jobs_booking ON no_show_prompt_jobs (booking_id)",
	"CREATE INDEX IF NOT EXISTS ix_no_show_prompt_jobs_status ON no_show_prompt_jobs (status)",
	# Same least-privilege posture as booking_reminder_jobs — no DELETE for
	# either role, a job row is never removed, only status-transitioned.
	"REVOKE DELETE ON no_show_prompt_jobs FROM blackink_app",
	"REVOKE DELETE ON no_show_prompt_jobs FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON no_show_prompt_jobs TO blackink_app",
	"GRANT USAGE ON SEQUENCE no_show_prompt_jobs_prompt_job_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON no_show_prompt_jobs TO blackink_system",
	"GRANT USAGE ON SEQUENCE no_show_prompt_jobs_prompt_job_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_no_show_prompt_jobs: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
