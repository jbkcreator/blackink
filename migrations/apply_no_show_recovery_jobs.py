"""
Provision no_show_recovery_jobs (Subtask 3.2.3 — No-Show Handler).

Outbox for the no-show recovery email — src/services/slack/listeners.py's
mark_no_show click handler INSERTs a row here (PENDING) instead of
calling SMTP inline, so the Slack click transaction never makes an
external network call. src/tasks/no_show_recovery_sender.py claims and
sends, mirroring show_rate_reminder_sender.py's SKIP LOCKED pattern.

triggered_at / sent_at exist specifically to prove the DoD's "recovery
flow email dispatched within 5 minutes of no-show trigger" — a test
asserts sent_at - triggered_at <= 5 minutes on a real row, not just that
the sweep interval is configured under 5 minutes.

Status lifecycle: PENDING -> SENDING -> SENT, with:
  FAILED     - bounded retry (transient send error).
  UNCERTAIN  - a send was attempted but the outcome couldn't be
               confirmed; never auto-retried (same distinction already
               implemented in calendar_confirmation.py / show_rate_reminders.py).
  CANCELLED  - re-checked before send and found the contact already
               rebooked (a later, distinct booking_id exists) — sending
               a recovery email to someone who already rebooked would be
               a wrong, unnecessary send.
  SKIPPED    - re-checked before send and found the meeting/booking
               record no longer eligible (e.g. the booking itself was
               cancelled after the job was enqueued).
  BLOCKED    - email sending is disabled (settings.email_sending_enabled)
               or no booking-link redirect target is available yet
               (resolve_booking_link() returned None) — same BLOCKED
               semantics as booking_reminder_jobs, excluded from the
               claim query, not silently dropped.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_no_show_recovery_jobs.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS no_show_recovery_jobs (
		recovery_job_id  BIGSERIAL    PRIMARY KEY,
		client_id        VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		booking_id       BIGINT       NOT NULL REFERENCES bookings(booking_id),
		contact_id       BIGINT       NOT NULL REFERENCES contacts(contact_id),
		status           VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
		attempts         INTEGER      NOT NULL DEFAULT 0,
		last_error       TEXT,
		next_retry_at    TIMESTAMPTZ,
		claimed_at       TIMESTAMPTZ,
		triggered_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		sent_at          TIMESTAMPTZ,
		created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_no_show_recovery_jobs_booking UNIQUE (booking_id),
		CONSTRAINT ck_no_show_recovery_jobs_status CHECK (
			status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'UNCERTAIN', 'CANCELLED', 'SKIPPED', 'BLOCKED')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_no_show_recovery_jobs_booking ON no_show_recovery_jobs (booking_id)",
	"CREATE INDEX IF NOT EXISTS ix_no_show_recovery_jobs_status ON no_show_recovery_jobs (status)",
	"REVOKE DELETE ON no_show_recovery_jobs FROM blackink_app",
	"REVOKE DELETE ON no_show_recovery_jobs FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON no_show_recovery_jobs TO blackink_app",
	"GRANT USAGE ON SEQUENCE no_show_recovery_jobs_recovery_job_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON no_show_recovery_jobs TO blackink_system",
	"GRANT USAGE ON SEQUENCE no_show_recovery_jobs_recovery_job_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_no_show_recovery_jobs: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
