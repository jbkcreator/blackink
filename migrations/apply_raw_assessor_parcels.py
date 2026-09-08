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

Akrash's own INSERT grant is NOT issued here — it lives in
apply_akrash_grant.py alongside the other two staging tables' grants.
apply_akrash_grant.py runs LAST (after RLS, per CLAUDE.md) and REVOKEs ALL
privileges from akrash_ingest before re-granting only its own explicit
list; a grant issued here would be silently wiped by that later REVOKE ALL
and never restored (a real bug an earlier version of this migration had —
Akrash could insert immediately after this migration ran, then lost
access again the moment the full sequence reached apply_akrash_grant.py,
with no error raised anywhere to surface it).

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
	# blackink_system: winback_ingest.py's assessor lookup runs BYPASSRLS,
	# same as promotion_sweep.py's reads of the other raw_prospect_* tables.
	# akrash_ingest's own grant lives in apply_akrash_grant.py — see the
	# module docstring above for why it must not be issued here.
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
