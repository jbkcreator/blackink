"""
Grant Akrash's restricted, INSERT-only access to the staging tables
(Dev 1 plan, migration 12 of 12 — final piece of AC #6).

Isolated in its own file, deliberately separate from
apply_raw_prospect_pipeline.py, so a third-party grant is independently
reviewable in its own diff. Everything else is explicitly revoked first —
akrash_ingest must never gain broader access via an accidental future
default-privilege grant.

Run LAST, after raw_prospect_companies/raw_prospect_contacts exist
(migration 7) and after RLS is enforced (migration 11) — akrash_ingest has
no RLS bypass and the two staging tables aren't in TENANT_POLICIES (Akrash
has no client visibility by design), so RLS doesn't gate this grant, but
running last keeps the sequence unambiguous: roles, then tables, then RLS,
then this narrowest-possible grant.

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
	unexpected = [g for g in grants if g.table_name not in ("raw_prospect_companies", "raw_prospect_contacts")]
	if unexpected:
		print("WARNING: akrash_ingest has grants beyond the two staging tables:", unexpected, file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
