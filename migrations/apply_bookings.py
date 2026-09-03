"""
Provision bookings + calendar_sync_queue (Dev 3, Subtask 3.2.1 —
Inbound Booking Engine).

`bookings` is the single shared ledger for both matched and unmatched
calendar bookings — replacing an earlier two-table draft (a separate
`unmatched_bookings` table) that was flagged during review as losing
shared audit-trail traceability. Both the matched-owner path and the
unmatched/queued-for-reconciliation path write here, via one atomic
INSERT ... ON CONFLICT keyed on (client_id, provider,
calendar_connection_id, external_event_id) — see
src/services/booking_ingest.py's sync_connection() for the exact
statement and how it classifies new/cancelled/rescheduled bookings from
a single query, avoiding the race a separate SELECT-then-INSERT would
have under concurrent/retried webhook deliveries.

confirmation_status defaults to NOT_REQUIRED, not PENDING — a blanket
PENDING default would queue a send attempt for every baseline-sync row
(pre-existing bookings that predate the calendar connection) and every
cancelled booking. It only becomes PENDING for a genuinely-new,
CONFIRMED, non-baseline booking (set explicitly by booking_ingest.py in
the same transaction as the insert), and is stepped through
SENDING/SENT/FAILED/FAILED_PERMANENT/UNCERTAIN/CANCELLED by
src/tasks/booking_confirmation_sender.py's atomic claim-and-send loop.

calendar_sync_queue is the ack-then-process decoupling point: the
webhook routes only validate the provider handshake and upsert a queue
row here (idempotent coalesce via ON CONFLICT on the connection_id
primary key), returning fast to satisfy both providers' response-time
expectations, while the actual sync runs out-of-band (a FastAPI
BackgroundTask for the common case, src/tasks/calendar_sync_worker.py as
the durability backstop).

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_bookings.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS bookings (
		booking_id                   BIGSERIAL    PRIMARY KEY,
		client_id                    VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		provider                     VARCHAR(20)  NOT NULL,
		calendar_connection_id       BIGINT       NOT NULL REFERENCES calendar_connections(connection_id),
		external_event_id            VARCHAR(255) NOT NULL,
		event_status                 VARCHAR(20)  NOT NULL,
		scheduled_at                 TIMESTAMPTZ,
		client_rep_name               VARCHAR(200),
		client_rep_email              VARCHAR(255),
		raw_payload                    JSONB        NOT NULL,
		owner_contact_id                BIGINT       REFERENCES owner_contacts(owner_contact_id),
		status                            VARCHAR(30)  NOT NULL DEFAULT 'PENDING_RECONCILIATION',
		confirmation_status                VARCHAR(20)  NOT NULL DEFAULT 'NOT_REQUIRED',
		confirmation_attempts                INTEGER      NOT NULL DEFAULT 0,
		confirmation_last_error               TEXT,
		confirmation_next_retry_at              TIMESTAMPTZ,
		confirmation_claimed_at                  TIMESTAMPTZ,
		created_at                                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at                                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_bookings_event_status CHECK (event_status IN ('CONFIRMED', 'CANCELLED')),
		CONSTRAINT ck_bookings_status CHECK (
			status IN ('MATCHED', 'PENDING_RECONCILIATION', 'CANCELLED')
		),
		CONSTRAINT ck_bookings_confirmation_status CHECK (
			confirmation_status IN (
				'NOT_REQUIRED', 'PENDING', 'SENDING', 'SENT', 'FAILED',
				'FAILED_PERMANENT', 'UNCERTAIN', 'CANCELLED'
			)
		),
		CONSTRAINT uq_bookings_identity UNIQUE (client_id, provider, calendar_connection_id, external_event_id)
	)
	""",
	# Self-correcting DROP+ADD (same pattern as apply_sms_dispatch_log.py) —
	# GOHIGHLEVEL added when the GHL fallback (client comment W1-8) was built.
	"ALTER TABLE bookings DROP CONSTRAINT IF EXISTS ck_bookings_provider",
	"ALTER TABLE bookings ADD CONSTRAINT ck_bookings_provider CHECK (provider IN ('GOOGLE', 'MICROSOFT', 'GOHIGHLEVEL'))",
	"CREATE INDEX IF NOT EXISTS ix_bookings_client ON bookings (client_id)",
	"CREATE INDEX IF NOT EXISTS ix_bookings_owner_contact ON bookings (owner_contact_id)",
	# Read by booking_confirmation_sender.py's claim query.
	"CREATE INDEX IF NOT EXISTS ix_bookings_confirmation_status ON bookings (confirmation_status)",
	# DELETE granted alongside SELECT/INSERT/UPDATE — same convention as
	# contacts/companies (apply_contacts.py, apply_companies.py), needed
	# for test-fixture teardown (tests/test_booking_engine_live.py), not
	# just app-role runtime writes.
	"GRANT SELECT, INSERT, UPDATE, DELETE ON bookings TO blackink_app",
	"GRANT USAGE ON SEQUENCE bookings_booking_id_seq TO blackink_app",
	# calendar_sync_worker.py / booking_confirmation_sender.py run as blackink_system.
	"GRANT SELECT, INSERT, UPDATE, DELETE ON bookings TO blackink_system",
	"GRANT USAGE ON SEQUENCE bookings_booking_id_seq TO blackink_system",
	"""
	CREATE TABLE IF NOT EXISTS calendar_sync_queue (
		connection_id  BIGINT       PRIMARY KEY REFERENCES calendar_connections(connection_id),
		requested_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
	)
	""",
	"GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_sync_queue TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_sync_queue TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_bookings: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
