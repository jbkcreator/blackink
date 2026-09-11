"""
Provision the counties reference table (Dev 1 plan, migration 2 of 11).

Canonical list of Florida counties Blackink targets — avoids free-text
county-name typos propagating into companies.county_slug /
county_allocations.county_slug / client_pm_books. First-launch counties are
Hillsborough and Pinellas; next-wave are Orange, Duval, Polk, Pasco, Lee,
Brevard, Volusia, Seminole. Miami-Dade is deliberately excluded from first
launch (spec lines 24 and 306). Add more counties by re-running this script
with an expanded SEED_COUNTIES list; the INSERT is idempotent (ON CONFLICT DO NOTHING).

Idempotent: CREATE TABLE IF NOT EXISTS, seed insert is ON CONFLICT DO NOTHING.

    PYTHONPATH=. python migrations/apply_counties.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS counties (
		county_slug VARCHAR(60) PRIMARY KEY,
		county_name VARCHAR(100) NOT NULL,
		state       VARCHAR(2)  NOT NULL
	)
	""",
	"GRANT SELECT ON counties TO blackink_app",
	# show_rate_reminder_sender.py (Subtask 3.2.2) runs as blackink_system
	# and joins counties for county_name in its reminder-content query
	# (src/services/show_rate_reminders.py's _load_job) — without this
	# grant, that join fails with InsufficientPrivilege the first time a
	# reminder job is actually claimed and sent.
	"GRANT SELECT ON counties TO blackink_system",
]

SEED_COUNTIES = [
	# First-launch
	{"county_slug": "hillsborough_fl", "county_name": "Hillsborough", "state": "FL"},
	{"county_slug": "pinellas_fl",     "county_name": "Pinellas",     "state": "FL"},
	# Next-wave
	{"county_slug": "orange_fl",       "county_name": "Orange",       "state": "FL"},
	{"county_slug": "duval_fl",        "county_name": "Duval",        "state": "FL"},
	{"county_slug": "polk_fl",         "county_name": "Polk",         "state": "FL"},
	{"county_slug": "pasco_fl",        "county_name": "Pasco",        "state": "FL"},
	{"county_slug": "lee_fl",          "county_name": "Lee",          "state": "FL"},
	{"county_slug": "brevard_fl",      "county_name": "Brevard",      "state": "FL"},
	{"county_slug": "volusia_fl",      "county_name": "Volusia",      "state": "FL"},
	{"county_slug": "seminole_fl",     "county_name": "Seminole",     "state": "FL"},
	# Miami-Dade deliberately excluded from first launch (spec lines 24 and 306)
]

SEED_SQL = """
	INSERT INTO counties (county_slug, county_name, state)
	VALUES (:county_slug, :county_name, :state)
	ON CONFLICT (county_slug) DO NOTHING
"""


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		for row in SEED_COUNTIES:
			db.execute(text(SEED_SQL), row)
		db.commit()
		count = db.execute(text("SELECT count(*) FROM counties")).scalar()
	print(f"apply_counties: done — {count} counties present")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
