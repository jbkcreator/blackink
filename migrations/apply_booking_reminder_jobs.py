"""
Provision booking_reminder_jobs (Subtask 3.2.2 — Show-Rate Reminder
Cascade).

Two jobs per INTERNAL_SALES_DEMO booking ('24h_email', '30min_email'),
scheduled as an absolute TIMESTAMPTZ computed directly from
bookings.scheduled_at at creation/reschedule time — no timezone math is
needed to decide *when* a job fires (see src/services/show_rate_reminders.py
for where timezone resolution actually matters: email *content*).

Status lifecycle: PENDING -> SENDING -> SENT, with FAILED (bounded
retry), CANCELLED (booking cancelled/rescheduled past this job),
SKIPPED (the window had already passed when the job was created/
rescheduled), BLOCKED (a real precondition is unmet right now — email
sending disabled, or Dev 2's Owner Visibility Score score/PDF isn't
ready yet), and UNCERTAIN (a send was attempted but the outcome
couldn't be confirmed, never auto-retried). BLOCKED/UNCERTAIN are
deliberately excluded from the sweep's claim query so a blocked job
doesn't become an infinitely-reclaimed PENDING row every tick — see
show_rate_reminder_sender.py's self-heal steps for how BLOCKED rows
actually recover.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_booking_reminder_jobs.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS booking_reminder_jobs (
		reminder_job_id  BIGSERIAL    PRIMARY KEY,
		client_id        VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		booking_id       BIGINT       NOT NULL REFERENCES bookings(booking_id),
		reminder_step    VARCHAR(20)  NOT NULL,
		scheduled_for    TIMESTAMPTZ  NOT NULL,
		status           VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
		attempts         INTEGER      NOT NULL DEFAULT 0,
		last_error       TEXT,
		next_retry_at    TIMESTAMPTZ,
		claimed_at       TIMESTAMPTZ,
		created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_booking_reminder_jobs_identity UNIQUE (booking_id, reminder_step),
		CONSTRAINT ck_booking_reminder_jobs_step CHECK (reminder_step IN ('24h_email', '30min_email')),
		CONSTRAINT ck_booking_reminder_jobs_status CHECK (
			status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELLED', 'SKIPPED', 'BLOCKED', 'UNCERTAIN')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_booking_reminder_jobs_booking ON booking_reminder_jobs (booking_id)",
	# Read by show_rate_reminder_sender.py's claim query and self-heal steps.
	"CREATE INDEX IF NOT EXISTS ix_booking_reminder_jobs_status ON booking_reminder_jobs (status)",
	# No DELETE for either role — least privilege. A reminder job is never
	# deleted in production; a cancelled/skipped job stays as a row with
	# status='CANCELLED'/'SKIPPED' for audit (schedule_show_rate_reminders(),
	# booking_ingest.py), never removed. Test-fixture teardown goes through
	# get_owner_db_context() instead of widening this grant. Explicit REVOKE
	# so this migration is self-correcting even against a database an
	# earlier draft's broader GRANT already ran against.
	"REVOKE DELETE ON booking_reminder_jobs FROM blackink_app",
	"REVOKE DELETE ON booking_reminder_jobs FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON booking_reminder_jobs TO blackink_app",
	"GRANT USAGE ON SEQUENCE booking_reminder_jobs_reminder_job_id_seq TO blackink_app",
	# show_rate_reminder_sender.py runs as blackink_system.
	"GRANT SELECT, INSERT, UPDATE ON booking_reminder_jobs TO blackink_system",
	"GRANT USAGE ON SEQUENCE booking_reminder_jobs_reminder_job_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_booking_reminder_jobs: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
