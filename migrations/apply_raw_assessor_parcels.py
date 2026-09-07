"""
Provision raw_assessor_parcels — Akrash's county tax assessor roll staging
feed (Subtask 3.1.1 — Lost-Owner CSV Ingest with Assessor & FRBO
Cross-Reference).

Mirrors raw_prospect_companies' posture (migrations/apply_raw_prospect_
pipeline.py): no client_id column, Akrash has no client-roster visibility,
INSERT-only for the akrash_ingest role. Ownership/eligibility is decided
entirely downstream by src/services/winback_ingest.py's lookup logic, never
by anything written here.

Not registered in config/tenant_policies.py — same "Akrash has no client
visibility by design" reasoning already documented there for
raw_prospect_companies / raw_prospect_contacts.

Idempotent: CREATE TABLE IF NOT EXISTS.
Run after apply_counties.py (FK target). Not tenant-bearing, so ordering
relative to apply_rls_policies.py doesn't matter, but keep it grouped with
the other staging-table migrations for readability.

    PYTHONPATH=. python migrations/apply_raw_assessor_parcels.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS raw_assessor_parcels (
		id                          BIGSERIAL    PRIMARY KEY,
		county_slug                 VARCHAR(60)  NOT NULL REFERENCES counties(county_slug),
		parcel_address_raw          TEXT         NOT NULL,
		parcel_address_normalized   VARCHAR(500) NOT NULL,
		owner_name_on_roll          TEXT         NOT NULL,
		assessor_parcel_id          VARCHAR(100),
		raw_payload                 JSONB        NOT NULL DEFAULT '{}'::jsonb,
		ingested_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW()
	)
	""",
	# The lookup key winback_ingest.py queries by — see that module's
	# _lookup_assessor_parcel().
	"CREATE INDEX IF NOT EXISTS ix_raw_assessor_parcels_lookup "
	"ON raw_assessor_parcels (county_slug, parcel_address_normalized)",
	# akrash_ingest: INSERT-only, same restricted posture as the two existing
	# raw_prospect_* staging tables — a compromised/misbehaving Akrash feed
	# can add rows but never read, modify, or delete what's already staged.
	"GRANT SELECT, INSERT ON raw_assessor_parcels TO akrash_ingest",
	"REVOKE UPDATE, DELETE ON raw_assessor_parcels FROM akrash_ingest",
	# blackink_system: winback_ingest.py's assessor lookup runs BYPASSRLS,
	# same as promotion_sweep.py's reads of the other raw_prospect_* tables.
	"GRANT SELECT ON raw_assessor_parcels TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'raw_assessor_parcels' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_raw_assessor_parcels: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
