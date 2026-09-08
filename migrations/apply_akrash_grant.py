"""
Grant Akrash's restricted, INSERT-only access to the staging tables
(Dev 1 plan, migration 12 of 12 — final piece of AC #6). Now three tables:
raw_prospect_companies / raw_prospect_contacts (original two) and
raw_assessor_parcels (Subtask 3.1.1's assessor-roll staging feed).

Isolated in its own file, deliberately separate from
apply_raw_prospect_pipeline.py / apply_raw_assessor_parcels.py, so a
third-party grant is independently reviewable in its own diff. Everything
else is explicitly revoked first — akrash_ingest must never gain broader
access via an accidental future default-privilege grant.

This is also the ONLY place akrash_ingest's grant on any staging table may
be issued — a GRANT for akrash_ingest inside a table-creation migration
would be silently wiped by this file's own REVOKE ALL the next time the
full sequence runs, and never restored (this happened for
raw_assessor_parcels: an earlier version of apply_raw_assessor_parcels.py
granted it directly, which worked right up until this migration ran,
after which Akrash lost access with no error raised anywhere).

Run LAST, after raw_prospect_companies/raw_prospect_contacts/
raw_assessor_parcels all exist and after RLS is enforced — akrash_ingest
has no RLS bypass and none of the three staging tables are in
TENANT_POLICIES (Akrash has no client visibility by design), so RLS
doesn't gate this grant, but running last keeps the sequence unambiguous:
roles, then tables, then RLS, then this narrowest-possible grant.

Idempotent: REVOKE ALL then targeted GRANTs — safely re-runnable.

    PYTHONPATH=. python migrations/apply_akrash_grant.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM akrash_ingest",
	"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM akrash_ingest",
	"GRANT USAGE ON SCHEMA public TO akrash_ingest",
	"GRANT INSERT ON raw_prospect_companies TO akrash_ingest",
	"GRANT INSERT ON raw_prospect_contacts TO akrash_ingest",
	"GRANT USAGE ON SEQUENCE raw_prospect_companies_id_seq TO akrash_ingest",
	"GRANT USAGE ON SEQUENCE raw_prospect_contacts_id_seq TO akrash_ingest",
	# Subtask 3.1.1 — INSERT-only, same restricted posture as the two tables
	# above; no SELECT, matching the "can add rows but never read what's
	# already staged" intent (blackink_system, not akrash_ingest, is the
	# one that reads this table back for the assessor lookup).
	"GRANT INSERT ON raw_assessor_parcels TO akrash_ingest",
	"GRANT USAGE ON SEQUENCE raw_assessor_parcels_id_seq TO akrash_ingest",
]

VERIFY_SQL = """
	SELECT table_name, privilege_type FROM information_schema.role_table_grants
	WHERE grantee = 'akrash_ingest' ORDER BY table_name, privilege_type
"""


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		grants = db.execute(text(VERIFY_SQL)).fetchall()
	print("apply_akrash_grant: done — akrash_ingest privileges:")
	for g in grants:
		print(f"  {g.table_name}: {g.privilege_type}")
	allowed_tables = ("raw_prospect_companies", "raw_prospect_contacts", "raw_assessor_parcels")
	unexpected = [g for g in grants if g.table_name not in allowed_tables]
	if unexpected:
		print("WARNING: akrash_ingest has grants beyond the three staging tables:", unexpected, file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
