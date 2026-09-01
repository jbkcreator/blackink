"""
Provision compliance_gate_checks and the is_claimed_by_other_client()
SQL function (Dev 1 plan, migration 9 of 12).

is_claimed_by_other_client is a SECURITY DEFINER function returning ONLY a
boolean — the requesting client's application code never learns WHICH other
client owns a company, just yes/no. This is the permanent, PMS-synced
non-poach check (queries client_pm_books), distinct from the 30-day-
reassessed county_allocations prospect-pool mechanism. See
src/services/compliance_gate.py and the Dev 1 plan's compliance-gate section.

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
	"""
	CREATE OR REPLACE FUNCTION is_claimed_by_other_client(
		p_company_id VARCHAR(64),
		p_requesting_client_id VARCHAR(40)
	)
	RETURNS BOOLEAN
	SECURITY DEFINER
	SET search_path = public
	LANGUAGE plpgsql
	AS $$
	DECLARE
		v_domain VARCHAR(255);
	BEGIN
		SELECT domain INTO v_domain FROM companies WHERE company_id = p_company_id;
		IF v_domain IS NULL THEN
			RETURN FALSE;
		END IF;
		RETURN EXISTS (
			SELECT 1 FROM client_pm_books b
			WHERE b.client_id <> p_requesting_client_id
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
	"GRANT EXECUTE ON FUNCTION is_claimed_by_other_client(VARCHAR, VARCHAR) TO blackink_app",
	# CREATE OR REPLACE FUNCTION does NOT transfer ownership if the function
	# already exists — only the first CREATE sets the owner. Explicit here so
	# this migration is self-correcting regardless of who created it first;
	# SECURITY DEFINER only bypasses RLS if the owner does (postgres/superuser).
	"ALTER FUNCTION is_claimed_by_other_client(VARCHAR, VARCHAR) OWNER TO postgres",
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
