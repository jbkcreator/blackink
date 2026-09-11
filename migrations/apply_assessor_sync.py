"""
Automated County Assessor Data Sync (Pinellas + Hillsborough) — replaces
the never-populated Akrash-staging assumption behind raw_assessor_parcels
with a first-party daily sync (src/tasks/assessor_sync.py). See
docs/plans/2026-09-11-automated-county-assessor-data-sync.md for the full
design rationale, source verification, and review history.

What this migration does:

1. Renames raw_assessor_parcels -> assessor_parcels. The table has never
   been written to (nothing in this repo ever granted Akrash a working
   INSERT path that survived apply_akrash_grant.py's REVOKE ALL — see that
   file's own docstring), so the rename is free: no data migration, just a
   name that stops implying an ownership model (Akrash) this repo never
   actually built.

2. Adds ~25 columns normalizing both counties' source files into one
   shape: parcel identity, owner/mailing detail, per-county use-code
   eligibility (property_class), the Pinellas-only government/agricultural
   exclusion (exemption_excluded), homestead status with change-detection,
   and source-tracking columns. `is_blackink_eligible` and
   `out_of_state_owner` are both STORED generated columns — see the plan's
   §1.1 for why splitting eligibility into `property_class` (owned only by
   the row-identifying import) and `exemption_excluded` (owned only by
   Pinellas's RP_EXEMPTIONS import) was the fix for a write-ownership bug
   found in review: two independently-scheduled Pinellas files upserting
   the same rows could otherwise violate NOT NULL constraints or silently
   revert each other's columns.

3. Converts the table from an append-only "latest wins" design
   (ORDER BY ingested_at DESC LIMIT 1, the original read strategy) to a
   real upsert target via UNIQUE (county_slug, assessor_parcel_id) — the
   append-only design cannot survive a weekly full-county refresh without
   the read cost growing forever.

4. Creates assessor_sync_state, the per-dataset source-tracking table
   (task doc §8): one row each for pinellas_fl/RP_PROPERTY_INFO,
   pinellas_fl/RP_EXEMPTIONS, hillsborough_fl/PARCEL_SPREADSHEET. Seeded
   idempotently via ON CONFLICT DO NOTHING.

Deliberate deviation from the plan doc's literal text: the plan's §1.1
says to rename the existing ix_raw_assessor_parcels_lookup index in place.
Implemented differently here — that index is DROPPED and superseded by the
new partial index ix_assessor_parcels_lookup_active (WHERE retired_at IS
NULL), because keeping both would leave two indexes on the identical
column pair for zero benefit (the partial index correctly subsumes the
non-partial one's every real use case for this table, since a retired
parcel must never be a Win-Back match). Safe because the table has never
held data in any environment.

Not tenant-bearing (no client_id anywhere) — no TENANT_POLICIES entry, no
RLS, same posture as raw_prospect_companies/raw_prospect_contacts. Run any
time after apply_counties.py (both new FKs target counties.county_slug);
ordering relative to apply_rls_policies.py doesn't matter, but keep it
grouped with the other staging-table migrations for readability.

Idempotent: DO $$ ... EXCEPTION WHEN ... blocks for the rename/constraint
additions that lack a native IF NOT EXISTS form, ADD COLUMN IF NOT EXISTS
for every new column, CREATE TABLE/INDEX IF NOT EXISTS elsewhere.

    PYTHONPATH=. python migrations/apply_assessor_sync.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	# ── 1. Rename the table (free — never held data) ───────────────────────
	"""
	DO $$ BEGIN
	  ALTER TABLE raw_assessor_parcels RENAME TO assessor_parcels;
	EXCEPTION WHEN undefined_table THEN NULL; END $$;
	""",
	"DROP INDEX IF EXISTS ix_raw_assessor_parcels_lookup",

	# ── 2. New columns — parcel identity / owner / mailing / use ───────────
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS strap VARCHAR(60)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS parcel_city VARCHAR(120)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS parcel_zip VARCHAR(20)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_name_secondary TEXT",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_address_1 TEXT",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_address_2 TEXT",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_city VARCHAR(120)",
	# Normalized to 2 letters at parse time — source columns are wider
	# (Hillsborough STATE is C(25) and can hold "FLORIDA").
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_state VARCHAR(2)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_state_raw VARCHAR(40)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_zip VARCHAR(20)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS owner_mailing_country VARCHAR(60)",
	# 2-digit state DOR code — the only cross-county-comparable use value.
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS property_use_code VARCHAR(2)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS property_use_raw TEXT",
	# Owned exclusively by the row-identifying import (RP_PROPERTY_INFO /
	# PARCEL_SPREADSHEET). NULL means "not a targetable use" per the
	# per-county allowlist in src/services/assessor/mapping.py.
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS property_class VARCHAR(30)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS parcel_status_raw VARCHAR(40)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS unit_count INTEGER",
	# Pinellas-only (RP_PROPERTY_INFO.ROLL_YEAR — "the year of the last
	# published roll"). Gap found while implementing importer.py: the plan
	# says to pin the RP_EXEMPTIONS join to this value, but nothing stored
	# it anywhere for that later join to reference. Hillsborough has no
	# equivalent field and never needs one (its homestead derivation reads
	# BASE directly, no year-window join) — NULL there by design, not a gap.
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS roll_year SMALLINT",

	# ── 3. New columns — homestead + Pinellas exemption exclusion ─────────
	# NOT NULL DEFAULT 'UNKNOWN': a parcel that has never been evaluated is
	# indistinguishable from one deliberately marked unknown, by design.
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_status VARCHAR(20) NOT NULL DEFAULT 'UNKNOWN'",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_raw_value TEXT",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_roll_year SMALLINT",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_next_year VARCHAR(20)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_use_pct SMALLINT",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_previous VARCHAR(20)",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS homestead_changed_at TIMESTAMPTZ",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS property_exemption_raw TEXT",
	# Owned exclusively by RP_EXEMPTIONS (Pinellas only). NULL = "not yet
	# evaluated" -> fails closed via is_blackink_eligible below. Hillsborough's
	# single import always sets this FALSE (no equivalent file there).
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS exemption_excluded BOOLEAN",

	# ── 4. New columns — source tracking / lifecycle ───────────────────────
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS source_dataset VARCHAR(60) NOT NULL DEFAULT ''",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS source_record_updated_at TIMESTAMPTZ",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS last_imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
	"ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ",

	# ── 5. Generated columns (must come after their dependencies exist) ────
	"""
	ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS out_of_state_owner BOOLEAN
	  GENERATED ALWAYS AS (
	    CASE WHEN owner_mailing_state IS NULL OR owner_mailing_state = '' THEN NULL
	         ELSE owner_mailing_state <> 'FL' END
	  ) STORED
	""",
	"""
	ALTER TABLE assessor_parcels ADD COLUMN IF NOT EXISTS is_blackink_eligible BOOLEAN
	  GENERATED ALWAYS AS (
	    property_class IS NOT NULL AND exemption_excluded IS FALSE
	  ) STORED
	""",

	# ── 6. Constraints ──────────────────────────────────────────────────────
	"ALTER TABLE assessor_parcels ALTER COLUMN assessor_parcel_id SET NOT NULL",
	"""
	DO $$ BEGIN
	  ALTER TABLE assessor_parcels ADD CONSTRAINT ck_assessor_parcels_homestead_status
	    CHECK (homestead_status IN ('HOMESTEAD', 'NO_HOMESTEAD', 'UNKNOWN', 'NOT_APPLICABLE'));
	EXCEPTION WHEN duplicate_object THEN NULL; END $$;
	""",
	"""
	DO $$ BEGIN
	  ALTER TABLE assessor_parcels ADD CONSTRAINT ck_assessor_parcels_homestead_previous
	    CHECK (homestead_previous IS NULL OR homestead_previous IN
	      ('HOMESTEAD', 'NO_HOMESTEAD', 'UNKNOWN', 'NOT_APPLICABLE'));
	EXCEPTION WHEN duplicate_object THEN NULL; END $$;
	""",
	"""
	DO $$ BEGIN
	  ALTER TABLE assessor_parcels ADD CONSTRAINT ck_assessor_parcels_homestead_next_year
	    CHECK (homestead_next_year IS NULL OR homestead_next_year IN
	      ('HOMESTEAD', 'NO_HOMESTEAD', 'UNKNOWN', 'NOT_APPLICABLE'));
	EXCEPTION WHEN duplicate_object THEN NULL; END $$;
	""",
	# The load-bearing schema change: converts the table from append-only
	# "latest wins" to a real upsert target. See module docstring point 3.
	#
	# Catches duplicate_table, not duplicate_object: a UNIQUE constraint's
	# backing index shares its name, and Postgres reports a name collision
	# on that index as "relation already exists" (duplicate_table /
	# SQLSTATE 42P07), a different error class than a duplicate CHECK
	# constraint name (duplicate_object / 42710) — confirmed by re-running
	# this migration against the live DB, which raised exactly this on the
	# second run before the fix.
	"""
	DO $$ BEGIN
	  ALTER TABLE assessor_parcels
	    ADD CONSTRAINT uq_assessor_parcels_county_parcel UNIQUE (county_slug, assessor_parcel_id);
	EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL; END $$;
	""",

	# ── 7. Indexes ───────────────────────────────────────────────────────────
	# The Win-Back lookup key. Partial: a retired parcel must never match —
	# supersedes the old non-partial ix_raw_assessor_parcels_lookup, dropped
	# above (see module docstring's "Deliberate deviation").
	"""
	CREATE INDEX IF NOT EXISTS ix_assessor_parcels_lookup_active
	  ON assessor_parcels (county_slug, parcel_address_normalized)
	  WHERE retired_at IS NULL
	""",
	# The retire query's own WHERE clause (importer.py's
	# import_row_owning_dataset) — found missing in review. Without this,
	# every nightly retire pass on Hillsborough's ~531k rows would have no
	# index covering (county_slug, source_dataset, last_seen_at), forcing
	# a scan of the whole county rather than seeking straight to the
	# handful of genuinely-stale rows.
	"""
	CREATE INDEX IF NOT EXISTS ix_assessor_parcels_retire_candidates
	  ON assessor_parcels (county_slug, source_dataset, last_seen_at)
	  WHERE retired_at IS NULL
	""",

	# ── 8. Grants ────────────────────────────────────────────────────────────
	# No DELETE: a parcel is soft-retired (retired_at), never row-deleted at
	# runtime — same posture apply_appointment_ops.py already documents for
	# its own tables. Nothing in importer.py/assessor_sync.py ever issues
	# DELETE FROM assessor_parcels (temp staging tables are DROPped, not
	# deleted-from); granting it anyway would be an unused, speculative
	# write surface. Revoked explicitly in case an earlier run of this
	# migration already granted it.
	"GRANT SELECT ON assessor_parcels TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON assessor_parcels TO blackink_system",
	"REVOKE DELETE ON assessor_parcels FROM blackink_system",
	# Explicit sequence grant, matching this repo's own established
	# convention (see apply_contacts.py's identical GRANT USAGE ON
	# SEQUENCE line for its BIGSERIAL PK) — found missing in review.
	# blackink_system's real INSERT path (importer.py's ON CONFLICT
	# upsert) calls nextval() on every brand-new parcel; without this
	# grant that would fail with "permission denied for sequence" on any
	# fresh database that doesn't happen to carry a broader ambient
	# default privilege the way this repo's long-lived dev DB does.
	# blackink_app never inserts into assessor_parcels, so it doesn't
	# need this. Still named raw_assessor_parcels_id_seq — confirmed live
	# (Postgres does NOT rename a table's sequences on ALTER TABLE RENAME,
	# exactly as this migration's own §1 docstring already warns).
	"GRANT USAGE ON SEQUENCE raw_assessor_parcels_id_seq TO blackink_system",

	# ── 9. assessor_sync_state — the §8 per-dataset source-tracking table ──
	"""
	CREATE TABLE IF NOT EXISTS assessor_sync_state (
	    id                       BIGSERIAL   PRIMARY KEY,
	    county_slug              VARCHAR(60) NOT NULL REFERENCES counties(county_slug),
	    dataset_name             VARCHAR(60) NOT NULL,
	    source_filename          TEXT,
	    -- last SUCCESSFULLY imported published timestamp; only advanced on success.
	    source_published_at      TIMESTAMPTZ,
	    -- latest timestamp SEEN on the page; may run ahead of the above while failing.
	    source_published_at_seen TIMESTAMPTZ,
	    last_checked_at          TIMESTAMPTZ,
	    last_downloaded_at       TIMESTAMPTZ,
	    last_imported_at         TIMESTAMPTZ,
	    import_status            VARCHAR(24) NOT NULL DEFAULT 'PENDING',
	    attempts                 SMALLINT    NOT NULL DEFAULT 0,
	    next_retry_at            TIMESTAMPTZ,
	    claimed_at               TIMESTAMPTZ,
	    http_etag                TEXT,
	    http_last_modified       TEXT,
	    file_hash                VARCHAR(64),
	    row_count                INTEGER,
	    last_error               TEXT,
	    -- Classifies WHY a FAILED/FAILED_PERMANENT row failed, so the
	    -- #blackink-qa alert can tell "will clear on retry" from "a human
	    -- needs to fix the scraper" without parsing last_error's free text.
	    failure_category         VARCHAR(30),
	    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
	    CONSTRAINT uq_assessor_sync_state UNIQUE (county_slug, dataset_name),
	    CONSTRAINT ck_assessor_sync_state_failure_category CHECK (failure_category IS NULL
	        OR failure_category IN (
	            'NETWORK_TIMEOUT', 'SITE_STRUCTURE_CHANGED', 'VALIDATION_FAILED',
	            'DOWNLOAD_SIZE_EXCEEDED', 'INTERNAL_ERROR'
	        )),
	    CONSTRAINT ck_assessor_sync_state_status CHECK (import_status IN (
	        'PENDING', 'CHECKING', 'DOWNLOADING', 'IMPORTING', 'SUCCESS',
	        'SKIPPED_UNCHANGED', 'FAILED', 'FAILED_PERMANENT'))
	)
	""",
	# No INSERT/DELETE: the three rows are seeded once by this migration
	# (owner role, below) and only ever UPDATEd afterward — _claim()/
	# _mark() in assessor_sync.py never insert or delete a row. Same
	# least-privilege reasoning as assessor_parcels' dropped DELETE grant
	# above; found in review by grepping for any runtime INSERT/DELETE
	# against this table and finding none.
	#
	# EXPLICIT REVOKE, not just a narrower GRANT — found live: this
	# database carries a schema-wide default privilege (not documented
	# anywhere in this repo's own migrations) that auto-grants
	# blackink_system ALL privileges on any newly-created table the
	# instant CREATE TABLE runs. A plain "GRANT SELECT, UPDATE" is purely
	# additive and does NOT remove what the default already granted —
	# confirmed by re-querying information_schema.role_table_grants after
	# applying an earlier version of this migration that omitted the
	# REVOKEs and still showed INSERT/DELETE present. REVOKE is the only
	# statement that actually overrides it (already proven to work for
	# assessor_parcels' DELETE above, since that table pre-dates whichever
	# default-privilege rule this database has).
	"GRANT SELECT, UPDATE ON assessor_sync_state TO blackink_system",
	"REVOKE INSERT, DELETE ON assessor_sync_state FROM blackink_system",
	"""
	INSERT INTO assessor_sync_state (county_slug, dataset_name)
	VALUES
	    ('pinellas_fl', 'RP_PROPERTY_INFO'),
	    ('pinellas_fl', 'RP_EXEMPTIONS'),
	    ('hillsborough_fl', 'PARCEL_SPREADSHEET')
	ON CONFLICT (county_slug, dataset_name) DO NOTHING
	""",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		parcel_cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'assessor_parcels' ORDER BY ordinal_position"
			)
		).fetchall()
		sync_rows = db.execute(
			text("SELECT county_slug, dataset_name FROM assessor_sync_state ORDER BY id")
		).fetchall()
	print("apply_assessor_sync: assessor_parcels columns —", [c.column_name for c in parcel_cols])
	print("apply_assessor_sync: assessor_sync_state seeded —", [(r.county_slug, r.dataset_name) for r in sync_rows])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
