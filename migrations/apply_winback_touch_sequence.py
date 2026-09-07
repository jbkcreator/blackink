"""
Provision the Three-Touch Win-Back Sequence's own tables/columns (Subtask
3.1.2 — builds on 3.1.1's winback_imports/winback_rows). Adds:

  - winback_rows.stopped_at / stop_reason — set the instant a reply,
    opt-out, or meeting_booked event stops a run (see
    src/services/winback_sequencer.py::stop_active_winback_runs).
  - winback_rows.audit_loss_dollars_est — Touch 1 personalization field.
    No task anywhere computes this value yet; the column is added now,
    nullable, so a future scoring step needs no migration of its own.
  - winback_touch_dispatches (new, tenant-bearing) — the at-most-once
    dispatch claim table, mirrors sequence_touch_dispatches exactly but
    keyed on winback_row_id instead of run_id (winback owners are never
    linked to sequence_runs — see 3.1.1's own plan doc for why).
  - calendar_connections.is_default_owner_booking (+ a per-client partial
    unique index, CLIENT_OWNER_BOOKING scope) — lets Touch 3 resolve a
    working booking link into the client's own calendar, mirroring
    is_default_sales_booking's existing INTERNAL_SALES_DEMO pattern.
  - winback_gate_checks (new, tenant-bearing) — the audit-trail counterpart
    to compliance_gate_checks for the win-back touch gate
    (src/services/winback_sequencer.py::evaluate_winback_touch_gate).
    A SEPARATE table, not a reuse of compliance_gate_checks: that table's
    contact_id column is a hard FK to contacts(contact_id), and a
    winback_row_id is never a valid contact_id — inserting one there would
    either violate the FK or, worse, silently attribute a compliance check
    to an unrelated contact by numeric coincidence. Same
    check_name/status/detail shape, no ABSTAIN status (the win-back gate
    is fully deterministic — it re-reads winback_rows' own durable
    suppression columns, it never ABSTAINs pending an external vendor
    lookup the way the cold-sequence gate's DNC check can).
  - Widens ck_winback_rows_suppression_reason (3.1.1) to also allow
    'DNC_UNVERIFIED' — a confirmed review finding: a row whose DNC status
    couldn't be affirmatively verified (missing vendor key, a batch call
    failure, a batch response omitting this phone, or a phone that never
    normalizes to any digits) previously only got requires_human_review =
    TRUE, and nothing downstream (the /arm endpoint's SQL,
    evaluate_winback_touch_gate) inspects that column — only
    suppression_state. src/services/winback_ingest.py's _flag_unscrubbed
    now sets suppression_state = TRUE with this reason, fail-closed, so
    such a row can never be armed until a human clears it.

winback_touch_dispatches and winback_gate_checks are both tenant-bearing
and registered in config/tenant_policies.py in the same change that adds
this migration — MUST run before apply_rls_policies.py or they get neither
RLS enforcement nor leakage-test coverage.

Idempotent: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS.
Run after apply_winback_imports.py and apply_calendar_connections.py,
before apply_rls_policies.py.

See docs/plans/2026-09-07-subtask-3.1.2-three-touch-winback-sequence.md
for the full design rationale.

    PYTHONPATH=. python migrations/apply_winback_touch_sequence.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS stopped_at TIMESTAMPTZ",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS stop_reason VARCHAR(20)",
	"ALTER TABLE winback_rows ADD COLUMN IF NOT EXISTS audit_loss_dollars_est INTEGER",
	# ADD CONSTRAINT has no IF NOT EXISTS in Postgres — wrap in a DO block
	# so re-running this migration against an already-migrated DB is a
	# no-op instead of an error, same idempotency posture as every other
	# statement here.
	"""
	DO $$ BEGIN
		ALTER TABLE winback_rows ADD CONSTRAINT ck_winback_rows_stop_reason
			CHECK (stop_reason IS NULL OR stop_reason IN ('REPLY', 'OPT_OUT', 'MEETING_BOOKED'));
	EXCEPTION WHEN duplicate_object THEN NULL; END $$;
	""",
	# Widen apply_winback_imports.py's ck_winback_rows_suppression_reason
	# (3.1.1) to also allow 'DNC_UNVERIFIED' — src/services/winback_ingest.py's
	# _flag_unscrubbed now sets this reason whenever a row's DNC status
	# couldn't be affirmatively verified (missing vendor key, a batch call
	# failure, a batch response omitting this phone, or a phone that never
	# normalizes at all), so it's never armable until a human clears it
	# (confirmed review finding — requires_human_review alone was not
	# enough, since nothing downstream inspects that column). DROP+ADD, not
	# an in-place ALTER — Postgres has no ALTER CONSTRAINT for CHECK value
	# lists; DROP CONSTRAINT IF EXISTS makes this idempotent on a re-run.
	"ALTER TABLE winback_rows DROP CONSTRAINT IF EXISTS ck_winback_rows_suppression_reason",
	"""
	ALTER TABLE winback_rows ADD CONSTRAINT ck_winback_rows_suppression_reason CHECK (
		suppression_reason IS NULL OR suppression_reason IN ('DNC_LISTED', 'NON_POACH_MATCH', 'SOLD', 'DNC_UNVERIFIED')
	)
	""",
	"""
	CREATE TABLE IF NOT EXISTS winback_touch_dispatches (
		dispatch_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id        VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		winback_row_id   BIGINT       NOT NULL REFERENCES winback_rows(winback_row_id),
		touch_step       SMALLINT     NOT NULL,
		status           VARCHAR(20)  NOT NULL DEFAULT 'SENDING',
		message_id       TEXT,
		mailbox_id       INTEGER,
		sent_at          TIMESTAMPTZ,
		created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT chk_winback_dispatch_status CHECK (status IN ('SENDING', 'SENT', 'FAILED')),
		CONSTRAINT uq_winback_dispatch_one_per_touch UNIQUE (winback_row_id, touch_step)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_wtd_client ON winback_touch_dispatches (client_id)",
	"CREATE INDEX IF NOT EXISTS ix_wtd_row ON winback_touch_dispatches (winback_row_id)",
	"CREATE INDEX IF NOT EXISTS ix_wtd_status ON winback_touch_dispatches (status, created_at)",
	"GRANT SELECT, INSERT, UPDATE ON winback_touch_dispatches TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON winback_touch_dispatches TO blackink_system",
	"ALTER TABLE calendar_connections ADD COLUMN IF NOT EXISTS is_default_owner_booking BOOLEAN NOT NULL DEFAULT FALSE",
	# Per-client default (unlike is_default_sales_booking's single global
	# default) — each PM-firm client has its own calendar.
	"""
	CREATE UNIQUE INDEX IF NOT EXISTS uq_calendar_connections_one_default_owner_booking
		ON calendar_connections (client_id)
		WHERE is_default_owner_booking = TRUE AND connection_scope = 'CLIENT_OWNER_BOOKING'
	""",
	"""
	CREATE TABLE IF NOT EXISTS winback_gate_checks (
		id              BIGSERIAL    PRIMARY KEY,
		winback_row_id  BIGINT       NOT NULL REFERENCES winback_rows(winback_row_id),
		client_id       VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		check_name      VARCHAR(60)  NOT NULL,
		status          VARCHAR(10)  NOT NULL,
		detail          TEXT,
		checked_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_winback_gate_checks_status CHECK (status IN ('PASS', 'FAIL'))
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_winback_gate_checks_row ON winback_gate_checks (winback_row_id, checked_at)",
	"CREATE INDEX IF NOT EXISTS ix_winback_gate_checks_client ON winback_gate_checks (client_id, checked_at)",
	"GRANT SELECT, INSERT ON winback_gate_checks TO blackink_app",
	"GRANT USAGE ON SEQUENCE winback_gate_checks_id_seq TO blackink_app",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'winback_touch_dispatches' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_winback_touch_sequence: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
