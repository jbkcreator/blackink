"""
Provision winback_imports / winback_rows — the Lost-Owner CSV ingest and
disposition tables (Subtask 3.1.1 — Lost-Owner CSV Ingest with Assessor &
FRBO Cross-Reference).

Standalone from companies/contacts and owner_contacts by design — see
docs/plans/2026-09-07-subtask-3.1.1-winback-csv-ingest-assessor-frbo.md's
"Current state" section for why none of the existing owner/company tables
fit a person x property CSV row (companies.domain is NOT NULL UNIQUE;
contacts is capped at two PM-firm-staff roles per company; owner_contacts'
own consumer only looks up rows, never creates them, and has no unique
constraint to build an upsert against).

Both tables are tenant-bearing (client_id) — registered in
config/tenant_policies.py in the same change that adds this migration, so
apply_rls_policies.py picks them up automatically. A tenant-bearing table
NOT registered there gets neither RLS enforcement nor leakage-test
coverage — there is no automatic net for that (see that module's own
docstring).

Idempotent: CREATE TABLE IF NOT EXISTS.
Run after apply_clients.py and apply_counties.py (FK targets), before
apply_rls_policies.py.

    PYTHONPATH=. python migrations/apply_winback_imports.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS winback_imports (
		import_id     UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id     VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		filename      TEXT         NOT NULL,
		uploaded_by   VARCHAR(200) NOT NULL,
		row_count     INTEGER      NOT NULL DEFAULT 0,
		status        VARCHAR(20)  NOT NULL DEFAULT 'PROCESSING',
		created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		completed_at  TIMESTAMPTZ,
		CONSTRAINT ck_winback_imports_status CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED'))
	)
	""",
	# client_id is denormalized onto winback_rows (not just reachable via a
	# join to winback_imports) because config/tenant_policies.py's "direct"
	# RLS mode needs its own column per table — same precedent documented on
	# meeting_outcome_prompt_jobs (copied from bookings.client_id at
	# schedule time).
	"""
	CREATE TABLE IF NOT EXISTS winback_rows (
		winback_row_id                BIGSERIAL    PRIMARY KEY,
		import_id                     UUID         NOT NULL REFERENCES winback_imports(import_id) ON DELETE CASCADE,
		client_id                     VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		owner_name                    VARCHAR(200) NOT NULL,
		property_address_raw          TEXT         NOT NULL,
		property_address_normalized   VARCHAR(500) NOT NULL,
		county_slug                   VARCHAR(60)  REFERENCES counties(county_slug),
		phone                         VARCHAR(20),
		email                         VARCHAR(255),
		still_owns                    BOOLEAN,
		still_renting                 BOOLEAN,
		disposition                   VARCHAR(30)  NOT NULL DEFAULT 'PENDING',
		requires_human_review         BOOLEAN      NOT NULL DEFAULT FALSE,
		validation_error              TEXT,
		assessor_checked_at           TIMESTAMPTZ,
		frbo_checked_at               TIMESTAMPTZ,
		dnc_clean                     BOOLEAN,
		dnc_checked_at                TIMESTAMPTZ,
		suppression_state             BOOLEAN      NOT NULL DEFAULT FALSE,
		suppression_reason            VARCHAR(50),
		created_at                    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at                    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_winback_rows_disposition CHECK (
			disposition IN ('PENDING', 'STILL_OWNS_STILL_RENTING', 'STILL_OWNS_NOT_RENTING', 'SOLD', 'UNKNOWN')
		),
		CONSTRAINT ck_winback_rows_suppression_reason CHECK (
			suppression_reason IS NULL OR suppression_reason IN ('DNC_LISTED', 'NON_POACH_MATCH')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_winback_rows_import ON winback_rows (import_id)",
	"CREATE INDEX IF NOT EXISTS ix_winback_rows_client ON winback_rows (client_id)",
	"CREATE INDEX IF NOT EXISTS ix_winback_rows_disposition ON winback_rows (client_id, disposition)",
	# blackink_app: the admin router (src/api/winback_router.py) writes these
	# tables under a tenant-scoped session, same convention as sequence_runs.
	"GRANT SELECT, INSERT, UPDATE ON winback_imports TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON winback_rows TO blackink_app",
	"GRANT USAGE ON SEQUENCE winback_rows_winback_row_id_seq TO blackink_app",
	# blackink_system: winback_ingest.py's pipeline (assessor lookup,
	# non-poach cross-client check, DNC batch scrub) runs BYPASSRLS, same
	# justification as sequence_enrollment.may_enroll's cross-client check.
	"GRANT SELECT, INSERT, UPDATE ON winback_imports TO blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON winback_rows TO blackink_system",
	"GRANT USAGE ON SEQUENCE winback_rows_winback_row_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'winback_rows' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_winback_imports: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
