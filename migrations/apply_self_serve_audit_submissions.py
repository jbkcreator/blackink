"""
Provision self_serve_audit_submissions (Subtask 3.2.3 — Owner Score
Self-Serve Landing Page).

Staging table for the public /audit landing page's domain-submission
form — mirrors raw_prospect_companies/raw_prospect_contacts' staging
philosophy (apply_raw_prospect_pipeline.py): the public POST handler's
entire job is to validate and insert one row here, nothing else. A
background worker (src/tasks/self_serve_audit_worker.py) is the only
thing that ever creates/finds a companies row or calls the Google
Places API, running under the BYPASSRLS system role — the public
request path never touches companies directly.

Deliberately NOT tenant-bearing (no client_id column) — this is
pre-company, pre-tenant lead data, same posture as raw_prospect_*.
Not registered in config/tenant_policies.py for the same reason.

visitor_name/visitor_email/visitor_company are NOT written into
contacts — Contact is DB-capped at exactly two roles per company
(migrations/apply_contacts.py:48-49) and a public visitor's submission
must never collide with or overwrite a real prospected contact.

county_slug is required at the API level (the landing page's form
includes a dropdown of the real, currently-launched counties, fetched
from GET /api/v1/public/counties) — not optional metadata. Both
companies.county_slug (apply_companies.py) and
owner_visibility_scores.county_slug (apply_owner_visibility_scores.py)
are themselves NOT NULL, so a self-serve domain outside a launched
county genuinely cannot be scored in this schema; there is no
domain-to-county geocoding step anywhere in this repo to infer one.
self_serve_audit_worker.py checks this before ever attempting a
companies INSERT (which would otherwise fail its own NOT NULL
constraint) and marks such a row BLOCKED_MISSING_COUNTY rather than
fabricating a county or crashing.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_self_serve_audit_submissions.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS self_serve_audit_submissions (
		submission_id      BIGSERIAL    PRIMARY KEY,
		domain_submitted   VARCHAR(255) NOT NULL,
		domain_normalized  VARCHAR(255) NOT NULL,
		company_id         VARCHAR(64)  REFERENCES companies(company_id),
		visitor_name       VARCHAR(200),
		visitor_email      VARCHAR(255),
		visitor_company    VARCHAR(200),
		county_slug        VARCHAR(60)  REFERENCES counties(county_slug),
		submitted_ip_hash  VARCHAR(64),
		status             VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
		attempts           INTEGER      NOT NULL DEFAULT 0,
		last_error         TEXT,
		next_retry_at      TIMESTAMPTZ,
		claimed_at         TIMESTAMPTZ,
		created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_self_serve_audit_submissions_status CHECK (
			status IN (
				'PENDING', 'SENDING', 'SCORED', 'SKIPPED_RECENT', 'FAILED',
				'FAILED_PERMANENT', 'REJECTED_DOMAIN', 'BLOCKED_MISSING_COUNTY'
			)
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_self_serve_audit_submissions_status ON self_serve_audit_submissions (status)",
	"CREATE INDEX IF NOT EXISTS ix_self_serve_audit_submissions_domain ON self_serve_audit_submissions (domain_normalized)",
	# No DELETE — a submission row is never removed, only status-transitioned.
	"REVOKE DELETE ON self_serve_audit_submissions FROM blackink_app",
	"REVOKE DELETE ON self_serve_audit_submissions FROM blackink_system",
	# blackink_app: the public router inserts PENDING/REJECTED_DOMAIN rows
	# with no client_id context (this table has none, and carries no RLS
	# policy — it is not registered in config/tenant_policies.py).
	"GRANT SELECT, INSERT, UPDATE ON self_serve_audit_submissions TO blackink_app",
	"GRANT USAGE ON SEQUENCE self_serve_audit_submissions_submission_id_seq TO blackink_app",
	# blackink_system: self_serve_audit_worker.py claims and processes rows.
	"GRANT SELECT, INSERT, UPDATE ON self_serve_audit_submissions TO blackink_system",
	"GRANT USAGE ON SEQUENCE self_serve_audit_submissions_submission_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_self_serve_audit_submissions: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
