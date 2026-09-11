"""
Add outbound_pause_until to contacts (S-10, W2 §3.2.1 — LATER-intent
reactivation).

contacts.outbound_paused_at / outbound_pause_reason already exist
(apply_bookings.py, Subtask 3.2.3's No-Show Handler) as an INDEFINITE pause
cleared only by an external event (rebooking). LATER needs a DATED pause —
"resume outreach on/after date X" — which is a different shape: this column
is the target resume date; src/tasks/reactivation_resume_sweep.py clears the
pause once NOW() >= outbound_pause_until, and ONLY when
outbound_pause_reason = 'REACTIVATION' (see that module's docstring), so it
can never resume a contact paused for a different reason (e.g. an overlapping
NO_SHOW_RECOVERY pause on the same contact).

No TENANT_POLICIES change needed — contacts is already registered (join mode
via companies.owning_client_id); this is an additive column on an
already-scoped table, not a new tenant-bearing table.

Idempotent: ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_reactivation_pause.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS outbound_pause_until TIMESTAMPTZ",
	# Sweep predicate: reason='REACTIVATION' AND pause_until <= NOW() AND pause_until IS NOT NULL.
	"CREATE INDEX IF NOT EXISTS ix_contacts_reactivation_due ON contacts (outbound_pause_until) "
	"WHERE outbound_pause_reason = 'REACTIVATION' AND outbound_pause_until IS NOT NULL",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_reactivation_pause: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
