"""
Per-client SLA-window overrides on `clients`.

The Reply-Triage escalation timers (first-response 15/60 min, tier-2 60 min,
tier-3 240 min) and the Speed-to-Lead 30-minute first-response window were all
fixed literals in code (`src/agents/respond/worker.py::_compute_sla`,
`src/tasks/respond_sla_sweep.py`'s tier queries,
`src/services/inbound_lead_orchestrator.py::_compute_sla_due`). This adds an
optional per-client override for each so a client on a stricter (or looser)
SLA can be configured without a code change.

Every column is NULLABLE with no default: NULL means "use the platform
default" (`config/settings.py::respond_sla_*` / `speed_to_lead_sla_minutes`),
so existing clients keep their historical behaviour untouched until an
operator sets an override. Additive columns on an already-registered,
already-RLS'd table — no TENANT_POLICIES change, no RLS re-push needed.

Idempotent: ADD COLUMN IF NOT EXISTS. Run any time after apply_clients.py.

    PYTHONPATH=. python migrations/apply_client_sla_windows.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE clients ADD COLUMN IF NOT EXISTS sla_hot_lead_minutes  INTEGER",
	"ALTER TABLE clients ADD COLUMN IF NOT EXISTS sla_standard_minutes  INTEGER",
	"ALTER TABLE clients ADD COLUMN IF NOT EXISTS sla_tier2_minutes     INTEGER",
	"ALTER TABLE clients ADD COLUMN IF NOT EXISTS sla_tier3_minutes     INTEGER",
	"ALTER TABLE clients ADD COLUMN IF NOT EXISTS speed_to_lead_sla_minutes INTEGER",
	# A configured override must be a positive number of minutes — a zero or
	# negative window would make every message instantly overdue. NULL (no
	# override) is still allowed; the CHECK only constrains real values.
	"""
	ALTER TABLE clients DROP CONSTRAINT IF EXISTS ck_clients_sla_minutes_positive
	""",
	"""
	ALTER TABLE clients ADD CONSTRAINT ck_clients_sla_minutes_positive CHECK (
		(sla_hot_lead_minutes      IS NULL OR sla_hot_lead_minutes      > 0) AND
		(sla_standard_minutes      IS NULL OR sla_standard_minutes      > 0) AND
		(sla_tier2_minutes         IS NULL OR sla_tier2_minutes         > 0) AND
		(sla_tier3_minutes         IS NULL OR sla_tier3_minutes         > 0) AND
		(speed_to_lead_sla_minutes IS NULL OR speed_to_lead_sla_minutes > 0)
	)
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
				"WHERE table_name = 'clients' AND column_name LIKE '%sla%minutes' "
				"ORDER BY column_name"
			)
		).fetchall()
	print(f"apply_client_sla_windows: done — {len(cols)} SLA columns on clients")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
