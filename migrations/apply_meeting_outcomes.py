"""meeting_outcomes — the upsertable current-state record behind the
60-second post-meeting Slack modal (blueprint §3.1.7 / split-doc Subtask
4.2.2). Deliberately a SEPARATE table from the append-only `events` ledger:
the DoD requires "submitting twice for the same meeting updates the
existing record, not two rows" — an append-only ledger structurally cannot
express that, so this table holds the current state and every write ALSO
calls src.services.events.log_event() for the immutable audit trail
(meeting_outcome_recorded).

Tenant-bearing (direct client_id column) — MUST be (and is, see
config/tenant_policies.py) registered before apply_rls_policies.py runs,
and its migration MUST run in .github/workflows/tenant_leakage_nightly.yml's
migration list before that, or tests/test_migration_coverage.py fails the
build.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_meeting_outcomes.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS meeting_outcomes (
		id                 BIGSERIAL    PRIMARY KEY,
		client_id          VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		contact_id         BIGINT       NOT NULL REFERENCES contacts(contact_id),
		meeting_occurred_at TIMESTAMPTZ NOT NULL,
		attendance_status  VARCHAR(20)  NOT NULL,
		pm_software        VARCHAR(50),
		door_count_est     INTEGER,
		objections         TEXT[]       NOT NULL DEFAULT '{}',
		next_action        VARCHAR(280),
		recorded_by        VARCHAR(100) NOT NULL,
		created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_meeting_outcomes_attendance
			CHECK (attendance_status IN ('Held','No-Show','Rescheduled')),
		CONSTRAINT uq_meeting_outcomes_meeting
			UNIQUE (client_id, contact_id, meeting_occurred_at)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcomes_client ON meeting_outcomes (client_id)",
	"CREATE INDEX IF NOT EXISTS ix_meeting_outcomes_contact ON meeting_outcomes (contact_id)",
	"GRANT SELECT, INSERT, UPDATE ON meeting_outcomes TO blackink_app",
	"GRANT USAGE ON SEQUENCE meeting_outcomes_id_seq TO blackink_app",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_meeting_outcomes: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
