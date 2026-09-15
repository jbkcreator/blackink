"""Attendance-proof columns on meeting_outcomes (Source of Truth item O-10).

Rule 2 of the future 4-rule billing gate (Week 4) needs durable proof that a
meeting really happened — a minimum duration with both parties present — not
just the human "Held" attestation `attendance_status` records today. No
mechanism was ever wired: no column captured a conference-provider reading.

This adds the storage for that proof so the capture point
(`src/services/meeting_outcomes.py`, via
`src/services/meeting_attendance_proof.py`'s provider seam) has somewhere to
write it, and the Week-4 gate has real evidence to read. Every column is
NULLABLE: today's only provider is the stub, which returns no reading, so a
manually-recorded outcome simply leaves these NULL (unproven) rather than
carrying a fabricated present/duration.

`attendance_proof_ref` is the external record's own id/url (a Google Meet
conferenceRecord name or Zoom meeting UUID) — the pointer a dispute reviewer
follows back to source; never a value this repo invents. `proof_source`
records which provider produced it (GOOGLE_MEET / ZOOM / MANUAL_ATTESTATION).

Additive columns on an already-registered, already-RLS'd tenant-bearing
table — no TENANT_POLICIES change, no RLS re-push. Idempotent: ADD COLUMN
IF NOT EXISTS. Run any time after apply_meeting_outcomes.py.

    PYTHONPATH=. python migrations/apply_meeting_attendance_proof.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE meeting_outcomes ADD COLUMN IF NOT EXISTS attendance_proof_ref      VARCHAR(300)",
	"ALTER TABLE meeting_outcomes ADD COLUMN IF NOT EXISTS proof_source              VARCHAR(40)",
	"ALTER TABLE meeting_outcomes ADD COLUMN IF NOT EXISTS meeting_duration_minutes  INTEGER",
	"ALTER TABLE meeting_outcomes ADD COLUMN IF NOT EXISTS both_parties_present      BOOLEAN",
	"ALTER TABLE meeting_outcomes ADD COLUMN IF NOT EXISTS attendance_proof_verified_at TIMESTAMPTZ",
	# A recorded duration is a real reading, so it can't be negative; NULL
	# (no reading) is always allowed — the CHECK only constrains real values.
	"ALTER TABLE meeting_outcomes DROP CONSTRAINT IF EXISTS ck_meeting_outcomes_duration_nonneg",
	"""
	ALTER TABLE meeting_outcomes ADD CONSTRAINT ck_meeting_outcomes_duration_nonneg
		CHECK (meeting_duration_minutes IS NULL OR meeting_duration_minutes >= 0)
	""",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'meeting_outcomes' "
				"  AND (column_name LIKE '%proof%' OR column_name LIKE '%parties%' "
				"       OR column_name = 'meeting_duration_minutes') "
				"ORDER BY column_name"
			)
		).fetchall()
	print(f"apply_meeting_attendance_proof: done — {len(cols)} proof columns on meeting_outcomes")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
