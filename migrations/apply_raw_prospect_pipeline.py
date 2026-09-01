"""
Provision raw_prospect_companies / raw_prospect_contacts — the Akrash
handoff staging tables (Dev 1 plan, migration 7 of 12; satisfies AC #6).

Two tables, not one flat row: each contact needs independently trackable
validation_status to support the confirmed "promote company with the one
clean contact" behavior. No client_id on either table — Akrash has no
visibility into the client roster; ownership is assigned only at promotion
time via the county-allocation join (see src/tasks/promotion_sweep.py).

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_raw_prospect_pipeline.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS raw_prospect_companies (
		id                     BIGSERIAL    PRIMARY KEY,
		company_id              VARCHAR(64)  NOT NULL,
		company_name             TEXT         NOT NULL,
		domain                   VARCHAR(255) NOT NULL,
		county_slug              VARCHAR(60)  REFERENCES counties(county_slug),
		door_count_est           INTEGER,
		door_count_source        VARCHAR(100),
		source_channel           VARCHAR(50),
		source_timestamp         TIMESTAMPTZ,
		submitted_by             VARCHAR(100) NOT NULL,
		enrichment_provider      VARCHAR(100),
		enrichment_timestamp     TIMESTAMPTZ,
		raw_payload              JSONB        NOT NULL DEFAULT '{}'::jsonb,
		validation_status        VARCHAR(20)  NOT NULL DEFAULT 'pending',
		reject_reason_code       VARCHAR(50),
		promoted_at              TIMESTAMPTZ,
		promoted_company_id      VARCHAR(64)  REFERENCES companies(company_id),
		created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_raw_prospect_companies_status CHECK (
			validation_status IN ('pending','cleared','quarantined','rejected')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_raw_prospect_companies_status ON raw_prospect_companies (validation_status)",
	"CREATE INDEX IF NOT EXISTS ix_raw_prospect_companies_company_id ON raw_prospect_companies (company_id)",
	# akrash_ingest gets INSERT-only in apply_akrash_grant.py (migration 12) —
	# kept separate so a third-party grant is independently reviewable.
	"GRANT SELECT, INSERT, UPDATE ON raw_prospect_companies TO blackink_app",
	"GRANT SELECT, UPDATE ON raw_prospect_companies TO blackink_system",
	"GRANT USAGE ON SEQUENCE raw_prospect_companies_id_seq TO blackink_app, blackink_system",
	"""
	CREATE TABLE IF NOT EXISTS raw_prospect_contacts (
		id                  BIGSERIAL    PRIMARY KEY,
		company_ref_id       BIGINT       NOT NULL REFERENCES raw_prospect_companies(id),
		role                 VARCHAR(20)  NOT NULL,
		name                 VARCHAR(200),
		email                VARCHAR(255),
		phone                VARCHAR(20),
		source               VARCHAR(100),
		validation_status    VARCHAR(20)  NOT NULL DEFAULT 'pending',
		reject_reason_code   VARCHAR(50),
		created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_raw_prospect_contacts_role CHECK (
			role IN ('OWNER_BROKER_MD','OFFICE_MANAGER_OPS')
		),
		CONSTRAINT ck_raw_prospect_contacts_status CHECK (
			validation_status IN ('pending','cleared','quarantined','rejected')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_raw_prospect_contacts_company_ref ON raw_prospect_contacts (company_ref_id)",
	"GRANT SELECT, INSERT, UPDATE ON raw_prospect_contacts TO blackink_app",
	"GRANT SELECT, UPDATE ON raw_prospect_contacts TO blackink_system",
	"GRANT USAGE ON SEQUENCE raw_prospect_contacts_id_seq TO blackink_app, blackink_system",
]


def main() -> int:
	with get_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_raw_prospect_pipeline: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
