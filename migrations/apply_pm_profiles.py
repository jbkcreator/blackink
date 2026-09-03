"""
Provision the pm_profiles table (Week 1, Subtask 1.1.1 — one operating
profile per client company).

*** geographic_coverage_counties is an ARRAY of county_slug values, NOT the
blueprint's literal geographic_coverage_polygon JSONB lat/lng field. ***

The client's own correction (blackink comments.txt, "Two corrections"):
"The unit is the COUNTY. Everywhere — data, rank, contract, seat. There is
no metro layer... A county is a hard boundary that already matches the
parcel data." Territory is therefore a set of counties a client is
contracted in, not a geographic polygon. Postgres has no native FK-on-
array-element constraint, so county membership is validated app-side
against counties.county_slug, not enforced here.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_pm_profiles.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS pm_profiles (
		profile_id                      BIGSERIAL     PRIMARY KEY,
		company_id                      VARCHAR(64)   NOT NULL UNIQUE
			REFERENCES companies(company_id) ON DELETE CASCADE,
		specialty_tags                  TEXT[]        NOT NULL DEFAULT '{}',
		languages_supported             TEXT[]        NOT NULL DEFAULT '{English}',
		asset_class_strengths           TEXT[]        NOT NULL DEFAULT '{Single Family,Small Multifamily}',
		geographic_coverage_counties    VARCHAR(60)[] NOT NULL DEFAULT '{}',
		historical_close_rate           NUMERIC(5,2)  NOT NULL DEFAULT 0.00,
		average_speed_to_lead_seconds   INTEGER       NOT NULL DEFAULT 0,
		show_rate_percentage            NUMERIC(5,2)  NOT NULL DEFAULT 0.00,
		created_at                      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		updated_at                      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
	)
	""",
	# company_id already carries a UNIQUE constraint (creates an index), but
	# named explicitly for lookup-by-company clarity, mirroring the other
	# Week 1 tables' index-naming convention.
	"CREATE UNIQUE INDEX IF NOT EXISTS ix_pm_profiles_company ON pm_profiles (company_id)",
	"GRANT SELECT, INSERT, UPDATE ON pm_profiles TO blackink_app",
	"GRANT USAGE ON SEQUENCE pm_profiles_profile_id_seq TO blackink_app",
	# promotion/onboarding batch jobs running as blackink_system may need to
	# seed or backfill profiles the same way they do companies.
	"GRANT SELECT, INSERT, UPDATE, DELETE ON pm_profiles TO blackink_system",
	"GRANT USAGE ON SEQUENCE pm_profiles_profile_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'pm_profiles' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_pm_profiles: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
