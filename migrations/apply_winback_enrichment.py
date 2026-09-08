"""
Owner-enrichment (skip-trace) state on winback_rows — Subtask 3.2.1,
Enrichment Pipeline Wiring Verification.

Client source: blackink-client-comments-04-09-2026.md:77 ("Confirm the
enrichment step (owner -> phone/email) for every signal"). Column names
enrichment_provider / enrichment_timestamp are verbatim from Blueprint v2's
Data Lineage & Provenance Metadata section (lines 45-46).

Adds:

  - winback_rows.email_status — same VERIFIED/ESTIMATED/UNVERIFIED/BOUNCED
    vocabulary as contacts.email_status (apply_contacts.py), deliberately
    not a new one.
  - winback_rows.email_previous — the address an enrichment overwrite
    superseded, so an inbound reply to the OLD address still stops the
    sequence (stop_active_winback_runs() in src/services/winback_sequencer.py
    is widened in the same change that introduces this column to match
    either email or email_previous).
  - winback_rows.phone_verified — nullable three-state (NULL = never
    checked, distinct from FALSE), same convention as the existing
    dnc_clean column.
  - winback_rows.requires_enrichment_review — the DoD's named column, read
    by evaluate_winback_touch_gate's new enrichment check.
  - winback_rows.enrichment_provider / enrichment_timestamp — Blueprint
    v2's provenance columns. enrichment_timestamp IS NULL is itself the
    pending-enrichment predicate — no separate status column.
  - winback_rows.enrichment_attempts — bounded-retry counter, same idiom as
    src/tasks/self_serve_audit_worker.py's own attempt-bounded retry.

winback_rows is already registered in config/tenant_policies.py (direct
client_id) and its RLS policy is column-agnostic, so this migration needs
NO TENANT_POLICIES entry and NO apply_rls_policies.py re-run. Table-level
GRANTs from apply_winback_imports.py already cover new columns.

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
Run after apply_winback_touch_sequence.py, before apply_rls_policies.py.

See docs/plans/2026-09-08-subtask-3.2.1-enrichment-pipeline-wiring-verification.md
for the full design rationale.

    PYTHONPATH=. python migrations/apply_winback_enrichment.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS email_status VARCHAR(20) NOT NULL DEFAULT 'UNVERIFIED'",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS email_previous VARCHAR(255)",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS requires_enrichment_review BOOLEAN NOT NULL DEFAULT FALSE",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS enrichment_provider VARCHAR(40)",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS enrichment_timestamp TIMESTAMPTZ",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS enrichment_attempts SMALLINT NOT NULL DEFAULT 0",
	# ADD CONSTRAINT has no IF NOT EXISTS in Postgres — wrap in a DO block so
	# re-running this migration against an already-migrated DB is a no-op
	# instead of an error, same idempotency posture as
	# apply_winback_touch_sequence.py's ck_winback_rows_stop_reason.
	"""
	DO $$ BEGIN
		ALTER TABLE winback_rows ADD CONSTRAINT ck_winback_rows_email_status
			CHECK (email_status IN ('VERIFIED', 'ESTIMATED', 'UNVERIFIED', 'BOUNCED'));
	EXCEPTION WHEN duplicate_object THEN NULL; END $$;
	""",
	# The enrichment sweep's claim query: outreach-eligible, not suppressed,
	# not yet enriched. Partial index keeps it cheap as a client's imported
	# row count grows and most rows are already enriched.
	"""
	CREATE INDEX IF NOT EXISTS ix_winback_rows_enrichment_pending
		ON winback_rows (client_id, disposition, winback_row_id)
		WHERE enrichment_timestamp IS NULL AND suppression_state = FALSE
	""",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'winback_rows' AND ("
				"  column_name LIKE 'enrichment%' "
				"  OR column_name IN ('email_status', 'email_previous', 'phone_verified')"
				") ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_winback_enrichment: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
