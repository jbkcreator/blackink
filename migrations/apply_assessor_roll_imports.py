"""
Provision assessor_roll_imports (S-24, W2 §3.2.4 A — county assessor roll
loader).

Audit trail for src/tasks/assessor_roll_refresh_sweep.py's monthly
change-detection sweep: one row per sweep tick per county, recording
whether the configured file was unchanged (no-op), imported (a new
file_sha256 triggered a full replace of that county's raw_assessor_parcels
rows), or failed (parse/validation error — existing raw_assessor_parcels
rows are left untouched, never partially wiped by a bad new file).

Not tenant-bearing — same reasoning as raw_assessor_parcels itself (no
client_id concept; this is platform-wide staging data).

Also grants blackink_system INSERT/DELETE on raw_assessor_parcels
(apply_raw_assessor_parcels.py granted it only SELECT, since Akrash was
originally the sole writer). S-24 reclassified this loader from an Akrash
dependency to our own dev work — our own scheduled sweep now writes there
directly, under blackink_system (BYPASSRLS), same role every other
platform-wide batch job in this repo uses.

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_assessor_roll_imports.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS assessor_roll_imports (
		id             BIGSERIAL     PRIMARY KEY,
		county_slug    VARCHAR(60)   NOT NULL REFERENCES counties(county_slug),
		file_path      TEXT          NOT NULL,
		file_sha256    VARCHAR(64),
		status         VARCHAR(20)   NOT NULL,
		row_count      INTEGER,
		error          TEXT,
		imported_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_assessor_roll_imports_status CHECK (
			status IN ('SUCCESS', 'UNCHANGED', 'FAILED', 'MISSING')
		)
	)
	""",
	# The sweep's own two lookups: "what's the last row for this county"
	# (staleness/hash check) and "was the last run for this county OK".
	"CREATE INDEX IF NOT EXISTS ix_assessor_roll_imports_county_time "
	"ON assessor_roll_imports (county_slug, imported_at DESC)",
	"GRANT SELECT, INSERT ON assessor_roll_imports TO blackink_system",
	"GRANT USAGE ON SEQUENCE assessor_roll_imports_id_seq TO blackink_system",
	# S-24: our own loader now writes raw_assessor_parcels directly (a full
	# per-county replace on each real file change) — previously only Akrash
	# could (SELECT-only was granted to blackink_system in
	# apply_raw_assessor_parcels.py, before this reclassification).
	"GRANT INSERT, DELETE ON raw_assessor_parcels TO blackink_system",
	# raw_assessor_parcels.id is BIGSERIAL — INSERT alone isn't enough,
	# blackink_system also needs USAGE on the backing sequence to actually
	# auto-generate an id (caught live: an INSERT under this role failed
	# with "permission denied for sequence" until this was added — exactly
	# the class of gap CLAUDE.md's own Blackink repository invariants
	# section warns is silent until the first live INSERT under the role).
	"GRANT USAGE ON SEQUENCE raw_assessor_parcels_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_assessor_roll_imports: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
