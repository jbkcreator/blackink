"""
Provision the contacts table (Dev 1 plan, migration 5 of 11).

Exactly two roles per company (OWNER_BROKER_MD, OFFICE_MANAGER_OPS), enforced
at the DB level via UNIQUE(company_id, contact_role_type) — not just
application convention. dnc_clean is nullable (NULL = never checked, distinct
from FALSE) with dnc_checked_at driving the compliance gate's ABSTAIN-on-stale
logic (see src/core/models.py's Contact docstring).

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS, plus a
self-correcting DROP+ADD CONSTRAINT for company_id's ON DELETE CASCADE
(added after the original table shipped without it — see Week 1 Subtask
1.1.1 verification).

    PYTHONPATH=. python migrations/apply_contacts.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS contacts (
		contact_id             BIGSERIAL    PRIMARY KEY,
		company_id              VARCHAR(64)  NOT NULL REFERENCES companies(company_id),
		contact_role_type       VARCHAR(20)  NOT NULL,
		first_name               VARCHAR(100),
		last_name                VARCHAR(100),
		title                     VARCHAR(150),
		email                     VARCHAR(255),
		email_status              VARCHAR(20)  NOT NULL DEFAULT 'UNVERIFIED',
		phone                     VARCHAR(20),
		phone_type                VARCHAR(20),
		linkedin_url              VARCHAR(500),
		is_opted_out              BOOLEAN      NOT NULL DEFAULT FALSE,
		dnc_clean                 BOOLEAN,
		dnc_checked_at            TIMESTAMPTZ,
		suppression_state         BOOLEAN      NOT NULL DEFAULT FALSE,
		compliance_eligibility    VARCHAR(30)  NOT NULL DEFAULT 'BLOCKED',
		last_outbound_touch_at    TIMESTAMPTZ,
		created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_contacts_role_type CHECK (
			contact_role_type IN ('OWNER_BROKER_MD','OFFICE_MANAGER_OPS')
		),
		CONSTRAINT ck_contacts_email_status CHECK (
			email_status IN ('VERIFIED','ESTIMATED','UNVERIFIED','BOUNCED')
		),
		CONSTRAINT ck_contacts_phone_type CHECK (
			phone_type IS NULL OR phone_type IN ('MOBILE','DIRECT_WORK','OFFICE_LANDLINE')
		),
		CONSTRAINT ck_contacts_compliance_eligibility CHECK (
			compliance_eligibility IN ('EMAIL_COLD_ELIGIBLE','TRANSACTIONAL_SMS_ONLY','BLOCKED')
		),
		CONSTRAINT ck_contacts_phone_e164 CHECK (
			phone IS NULL OR phone ~ '^\\+[1-9]\\d{1,14}$'
		),
		CONSTRAINT uq_contacts_company_role UNIQUE (company_id, contact_role_type)
	)
	""",
	"""
	CREATE UNIQUE INDEX IF NOT EXISTS ix_contacts_email
		ON contacts (email) WHERE email IS NOT NULL
	""",
	"CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts (company_id)",
	# Week 1 Subtask 1.1.1's idx_contacts_lookup requirement.
	"""
	CREATE INDEX IF NOT EXISTS idx_contacts_lookup
		ON contacts (email, company_id, compliance_eligibility)
	""",
	# CREATE TABLE IF NOT EXISTS above does not retrofit a constraint change
	# on an already-existing table — this table was originally created
	# without ON DELETE CASCADE on company_id. Re-run on every apply so it's
	# self-correcting regardless of what a prior version left in place.
	# Constraint name matches Postgres's own default naming
	# (<table>_<column>_fkey) since the original CREATE TABLE didn't name it.
	"ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_company_id_fkey",
	"""
	ALTER TABLE contacts
		ADD CONSTRAINT contacts_company_id_fkey
		FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE
	""",
	"GRANT SELECT, INSERT, UPDATE ON contacts TO blackink_app",
	"GRANT USAGE ON SEQUENCE contacts_contact_id_seq TO blackink_app",
	# promotion_sweep.py runs as blackink_system and promotes contacts
	# across every tenant's companies. DELETE included for the leakage-test
	# fixture's canary-row teardown (tests/fixtures/synthetic_tenants.py).
	"GRANT SELECT, INSERT, UPDATE, DELETE ON contacts TO blackink_system",
	"GRANT USAGE ON SEQUENCE contacts_contact_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'contacts' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_contacts: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
