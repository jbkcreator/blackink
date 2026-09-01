"""
Provision the companies table (Dev 1 plan, migration 4 of 11).

*** company_id is an APPLICATION-COMPUTED SHA-256 hex digest of the
normalized domain — never gen_random_uuid(). ***

The blueprint's own text contradicts itself here: Section 1.A's prose says
company_id is "a deterministic SHA-256 hash generated from normalized
domain," but Section 3.1.1's raw CREATE TABLE SQL reads
`company_id UUID PRIMARY KEY DEFAULT gen_random_uuid()`. These cannot both
be right — a random UUID would defeat the entire global-dedup /
non-poach-join design (see src/core/models.py's Company docstring and the
Dev 1 plan's "Key architectural decisions" §1). This migration deliberately
uses VARCHAR(64), no DEFAULT, matching the deterministic-hash design.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_companies.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS companies (
		company_id          VARCHAR(64)  PRIMARY KEY,
		company_name        TEXT         NOT NULL,
		website              VARCHAR(500),
		domain               VARCHAR(255) NOT NULL UNIQUE,
		county_slug          VARCHAR(60)  NOT NULL REFERENCES counties(county_slug),
		door_count_est       INTEGER,
		current_pm_software  VARCHAR(100),
		status               VARCHAR(20)  NOT NULL DEFAULT 'PROSPECTING',
		entity_type          VARCHAR(30)  NOT NULL DEFAULT 'single_property_owner',
		owning_client_id     VARCHAR(40)  REFERENCES clients(client_id),
		created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_companies_status CHECK (
			status IN ('PROSPECTING','ENGAGED','DEMO_BOOKED','CLIENT','EXCLUDED')
		),
		CONSTRAINT ck_companies_entity_type CHECK (
			entity_type IN ('single_property_owner','llc_portfolio_owner')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_companies_county_status ON companies (county_slug, status)",
	"CREATE INDEX IF NOT EXISTS ix_companies_owning_client ON companies (owning_client_id)",
	"GRANT SELECT, INSERT, UPDATE ON companies TO blackink_app",
]


def main() -> int:
	with get_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'companies' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_companies: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
