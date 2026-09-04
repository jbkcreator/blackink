"""
Provision the three Postgres roles the tenant-isolation chassis depends on
(Dev 1 plan, migration 1 of 11):

  blackink_app     — the application's normal runtime connection. RLS-subject
                      (no BYPASSRLS). Must NEVER be the schema-owning/migration
                      role, or Row-Level Security silently no-ops for it.
                      Per-table GRANTs are added by each subsequent
                      apply_<table>.py migration as that table is created —
                      this script only creates the role itself.

  blackink_system   — BYPASSRLS. Reserved exclusively for internal batch jobs
                      (promotion sweep, county-allocation reassessment, the
                      adversarial leakage-test harness). A CI grep-lint
                      asserts no file under src/api/ imports the code path
                      that connects as this role — request-handling code
                      must never reach the bypass.

  akrash_ingest     — no BYPASSRLS, no grants from this script. Migration 11
                      (apply_akrash_grant.py) grants INSERT-only on the two
                      raw_prospect_* staging tables once they exist — kept in
                      its own file so a third-party grant is independently
                      reviewable.

Idempotent: CREATE ROLE only if absent. Requires CREATEROLE/superuser to run
(same caveat as Forced Action's apply_vera_readonly_role.py, which this
migration's structure mirrors).

Refuses to run (writes nothing) if any of BLACKINK_APP_DB_PASSWORD,
BLACKINK_SYSTEM_DB_PASSWORD, AKRASH_INGEST_DB_PASSWORD is unset.

    PYTHONPATH=. python migrations/apply_db_roles.py
    PYTHONPATH=. python migrations/apply_db_roles.py --dry-run
"""
import argparse
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_owner_db_context

ROLES = {
	"blackink_app": {"bypassrls": False, "password_attr": "blackink_app_db_password"},
	"blackink_system": {"bypassrls": True, "password_attr": "blackink_system_db_password"},
	"akrash_ingest": {"bypassrls": False, "password_attr": "akrash_ingest_db_password"},
}

CREATE_ROLE_SQL_TEMPLATE = """
DO $$
BEGIN
   IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
      CREATE ROLE {role} LOGIN PASSWORD :pw {bypassrls_clause};
   END IF;
END $$;
"""

ROLE_EXISTS_SQL = "SELECT 1 FROM pg_roles WHERE rolname = :role"


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--dry-run", action="store_true")
	args = ap.parse_args()

	settings = get_settings()

	with get_owner_db_context() as db:
		if args.dry_run:
			for role in ROLES:
				exists = db.execute(text(ROLE_EXISTS_SQL), {"role": role}).scalar()
				print(
					f"dry-run: role {role!r} "
					f"{'already exists' if exists else 'would be created'} "
					f"(bypassrls={ROLES[role]['bypassrls']})"
				)
			return 0

		missing = [
			cfg["password_attr"]
			for cfg in ROLES.values()
			if not getattr(settings, cfg["password_attr"])
		]
		if missing:
			print(
				f"ERROR: missing required password setting(s): {missing} — "
				f"refusing to create/alter roles without explicit passwords.",
				file=sys.stderr,
			)
			return 1

		for role, cfg in ROLES.items():
			pw_secret = getattr(settings, cfg["password_attr"])
			pw = pw_secret.get_secret_value()
			bypassrls_clause = "BYPASSRLS" if cfg["bypassrls"] else "NOBYPASSRLS"
			stmt = CREATE_ROLE_SQL_TEMPLATE.format(role=role, bypassrls_clause=bypassrls_clause)
			db.execute(text(stmt), {"pw": pw})

		# Explicit, not relied-on-by-default: PG15+ already denies CREATE on
		# public to PUBLIC out of the box (confirmed for this project's
		# Postgres 16), but stating it here makes the invariant survive a
		# different Postgres version or a differently-provisioned database,
		# rather than depending on an upstream default nobody in this repo
		# chose. This is what makes SET search_path = pg_catalog on the
		# SECURITY DEFINER functions (resolve_calendar_connection,
		# resolve_sales_demo_target, is_claimed_by_other_client) an actual
		# guarantee instead of an assumption — none of blackink_app/
		# blackink_system/akrash_ingest can ever create a shadowing object
		# in a schema any of those functions might search.
		db.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
		db.commit()
		print(f"apply_db_roles: done — roles ready: {list(ROLES.keys())}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
