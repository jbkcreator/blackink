"""
Provision the us_area_code_timezones reference table (Dev 1 plan, Week 1
Subtask 1.2.2 — Quiet Hours).

Global, non-tenant reference data (same category as counties — NOT added
to config/tenant_policies.py). Maps a US NANP phone area code to the IANA
timezone used to compute "is it currently quiet hours (9pm-8am) for this
recipient". Seeded with a representative set spanning each US phone
timezone rather than the full ~300-code NANP list — a phone whose area
code isn't in this table fails closed (src/services/campaign_readiness_gate.py
withholds SMS eligibility rather than assuming it's not quiet hours).
Extend SEED_AREA_CODES and re-run to add more; the INSERT is idempotent
(ON CONFLICT DO NOTHING).

Idempotent: CREATE TABLE IF NOT EXISTS, seed insert is ON CONFLICT DO NOTHING.

    PYTHONPATH=. python migrations/apply_area_code_timezones.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS us_area_code_timezones (
		area_code     VARCHAR(3)   PRIMARY KEY,
		iana_timezone VARCHAR(50)  NOT NULL
	)
	""",
	"GRANT SELECT ON us_area_code_timezones TO blackink_app",
	"GRANT SELECT ON us_area_code_timezones TO blackink_system",
]

# Representative set spanning each US phone timezone, including the two
# already used as conversation examples (813 Tampa, 602 Phoenix).
SEED_AREA_CODES = [
	{"area_code": "212", "iana_timezone": "America/New_York"},
	{"area_code": "813", "iana_timezone": "America/New_York"},
	{"area_code": "305", "iana_timezone": "America/New_York"},
	{"area_code": "407", "iana_timezone": "America/New_York"},
	{"area_code": "904", "iana_timezone": "America/New_York"},
	{"area_code": "312", "iana_timezone": "America/Chicago"},
	{"area_code": "713", "iana_timezone": "America/Chicago"},
	{"area_code": "303", "iana_timezone": "America/Denver"},
	{"area_code": "602", "iana_timezone": "America/Phoenix"},  # No DST, unlike other Mountain-time codes.
	{"area_code": "415", "iana_timezone": "America/Los_Angeles"},
	{"area_code": "213", "iana_timezone": "America/Los_Angeles"},
]

SEED_SQL = """
	INSERT INTO us_area_code_timezones (area_code, iana_timezone)
	VALUES (:area_code, :iana_timezone)
	ON CONFLICT (area_code) DO NOTHING
"""


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		for row in SEED_AREA_CODES:
			db.execute(text(SEED_SQL), row)
		db.commit()
		count = db.execute(text("SELECT count(*) FROM us_area_code_timezones")).scalar()
	print(f"apply_area_code_timezones: done — {count} area codes present")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
