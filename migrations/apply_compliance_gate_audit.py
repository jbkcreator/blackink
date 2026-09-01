"""
Provision compliance_gate_checks and the is_claimed_by_other_client()
SQL function (Dev 1 plan, migration 9 of 12).

is_claimed_by_other_client is a SECURITY DEFINER function returning ONLY a
boolean — the requesting client's application code never learns WHICH other
client owns a company, just yes/no. This is the permanent, PMS-synced
non-poach check (queries client_pm_books), distinct from the 30-day-
reassessed county_allocations prospect-pool mechanism. See
src/services/compliance_gate.py and the Dev 1 plan's compliance-gate section.

Takes only company_id — the requesting client is read from the session's own
SET LOCAL app.current_client_id, not a caller-supplied parameter, so a shared
role can't invoke it under an arbitrary identity. EXECUTE is explicitly
revoked from PUBLIC (Postgres's default grant on every new function) and
granted only to blackink_app — akrash_ingest and every other role has no
path to this function at all.

Idempotent: CREATE TABLE IF NOT EXISTS, CREATE OR REPLACE FUNCTION.

    PYTHONPATH=. python migrations/apply_compliance_gate_audit.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS compliance_gate_checks (
		id          BIGSERIAL    PRIMARY KEY,
		contact_id  BIGINT       NOT NULL REFERENCES contacts(contact_id),
		client_id   VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		check_name  VARCHAR(60)  NOT NULL,
		status      VARCHAR(10)  NOT NULL,
		detail      TEXT,
		checked_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_compliance_gate_checks_status CHECK (status IN ('PASS','FAIL','ABSTAIN'))
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_compliance_gate_checks_contact ON compliance_gate_checks (contact_id, checked_at)",
	"CREATE INDEX IF NOT EXISTS ix_compliance_gate_checks_client ON compliance_gate_checks (client_id, checked_at)",
	"GRANT SELECT, INSERT ON compliance_gate_checks TO blackink_app",
	"GRANT USAGE ON SEQUENCE compliance_gate_checks_id_seq TO blackink_app",
	# Old 2-arg signature dropped, not just replaced: CREATE OR REPLACE cannot
	# change a function's parameter list, and the old signature's EXECUTE-to-
	# PUBLIC grant (Postgres's default on every new function, never explicitly
	# revoked here before) would otherwise survive as a live, unguarded
	# overload — exploitable by any role, including akrash_ingest, which has
	# no table grants at all but was still able to invoke this SECURITY
	# DEFINER function and probe cross-tenant non-poach claims through it.
	"DROP FUNCTION IF EXISTS is_claimed_by_other_client(VARCHAR, VARCHAR)",
	"""
	CREATE OR REPLACE FUNCTION is_claimed_by_other_client(
		p_company_id VARCHAR(64)
	)
	RETURNS BOOLEAN
	SECURITY DEFINER
	SET search_path = public
	LANGUAGE plpgsql
	AS $$
	DECLARE
		v_domain VARCHAR(255);
		v_requesting_client_id VARCHAR(40);
	BEGIN
		-- The requesting client is read from the session's own RLS tenant
		-- context (SET LOCAL app.current_client_id, set by session_scope()),
		-- never accepted as a parameter — a caller-supplied client_id let any
		-- session using the shared blackink_app role probe claim status under
		-- an arbitrary identity, including iterating candidate client_ids to
		-- infer WHICH client holds a claim, defeating the "boolean only, no
		-- identity disclosure" guarantee this function exists to provide.
		v_requesting_client_id := current_setting('app.current_client_id', true);
		IF v_requesting_client_id IS NULL OR v_requesting_client_id = '' THEN
			-- No tenant context to exclude — fail closed (treat as claimed)
			-- rather than evaluate an ambiguous "claimed by other than whom?".
			RETURN TRUE;
		END IF;

		SELECT domain INTO v_domain FROM companies WHERE company_id = p_company_id;
		IF v_domain IS NULL THEN
			RETURN FALSE;
		END IF;
		RETURN EXISTS (
			SELECT 1 FROM client_pm_books b
			WHERE b.client_id <> v_requesting_client_id
			AND (
				b.owner_domain = v_domain
				OR b.owner_email IN (
					SELECT email FROM contacts
					WHERE company_id = p_company_id AND email IS NOT NULL
				)
			)
		);
	END;
	$$
	""",
	# Postgres grants EXECUTE on every new function to PUBLIC by default —
	# revoke it explicitly so only blackink_app can call this. Re-run on every
	# apply so the grant is self-correcting regardless of what a prior version
	# of this migration left in place.
	"REVOKE EXECUTE ON FUNCTION is_claimed_by_other_client(VARCHAR) FROM PUBLIC",
	"GRANT EXECUTE ON FUNCTION is_claimed_by_other_client(VARCHAR) TO blackink_app",
	# CREATE OR REPLACE FUNCTION does NOT transfer ownership if the function
	# already exists — only the first CREATE sets the owner. Explicit here so
	# this migration is self-correcting regardless of who created it first;
	# SECURITY DEFINER only bypasses RLS if the owner does (superuser/BYPASSRLS).
	# CURRENT_USER, not a hardcoded role name — get_owner_db_context() is always
	# bound to settings.database_url, which is the migration-running superuser
	# in every environment (postgres locally/prod, blackink_owner in CI's
	# postgres:16 service, which POSTGRES_USER makes a superuser via initdb).
	"ALTER FUNCTION is_claimed_by_other_client(VARCHAR) OWNER TO CURRENT_USER",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_compliance_gate_audit: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
