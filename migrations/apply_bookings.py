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

	# ── Subtask 3.2.2 — Show-Rate Reminder Cascade ───────────────────────────
	# INTERNAL_SALES_DEMO-scope bookings (a PM firm prospect booking a sales
	# demo, matched against contacts/companies rather than owner_contacts —
	# see booking_ingest.py's connection_scope branch) resolve their target
	# here instead of owner_contact_id. Both nullable, both unused by
	# CLIENT_OWNER_BOOKING rows. Types verified against src/core/models.py:
	# Company.company_id is String(64) (models.py:191), Contact.contact_id
	# is BigInteger (models.py:251) — not assumed.
	"ALTER TABLE bookings ADD COLUMN IF NOT EXISTS target_company_id VARCHAR(64) REFERENCES companies(company_id)",
	"ALTER TABLE bookings ADD COLUMN IF NOT EXISTS target_contact_id BIGINT REFERENCES contacts(contact_id)",
	"CREATE INDEX IF NOT EXISTS ix_bookings_target_company ON bookings (target_company_id)",
	# contacts/companies are RLS-scoped via companies.owning_client_id, which
	# is NULL for every PROSPECTING/ENGAGED company (not yet allocated to a
	# client) — a session scoped to client_id='BLACKINK_INTERNAL_SALES' can
	# never see them directly, since NULL never matches an RLS equality
	# filter. Same SECURITY DEFINER escape hatch already used for
	# resolve_calendar_connection() (apply_calendar_connections.py) and the
	# non-poach check (is_claimed_by_other_client, apply_compliance_gate_audit.py)
	# — returns only the minimal identity needed, not a BYPASSRLS session
	# (which would also break the caller's enclosing advisory-locked
	# transaction by opening a second connection).
	#
	# Hardened to match is_claimed_by_other_client()'s established pattern,
	# not the weaker shape this function first shipped with in this branch's
	# own history: a bare SQL-language function with no caller-scope check
	# would let ANY session using the shared blackink_app role (every
	# tenant's session, not just BLACKINK_INTERNAL_SALES) probe arbitrary
	# emails against the global contacts table and learn whether a
	# match exists — a real cross-tenant information-disclosure hole this
	# migration closes before it ever ships. Caller scope is read from the
	# session's own RLS tenant context (SET LOCAL app.current_client_id),
	# never accepted as a parameter, for the identical reason
	# is_claimed_by_other_client() reads it that way rather than trusting a
	# caller-supplied client_id.
	#
	# search_path = pg_catalog (NOT public) — a second layer, not relying
	# solely on today's verified fact that blackink_app/blackink_system/
	# akrash_ingest have no CREATE on public (confirmed via
	# has_schema_privilege before landing this). pg_catalog is
	# superuser-owned and never writable by any application role in any
	# configuration, so it can't be shadowed regardless of what a future
	# grant or a different Postgres deployment default allows. Every
	# object this function touches is fully schema-qualified
	# (public.contacts) so removing public from the search path doesn't
	# break resolution — current_setting() itself resolves fine unqualified
	# since it's a pg_catalog builtin.
	"DROP FUNCTION IF EXISTS resolve_sales_demo_target(VARCHAR)",
	"""
	CREATE OR REPLACE FUNCTION resolve_sales_demo_target(p_email VARCHAR)
	RETURNS TABLE(contact_id BIGINT, company_id VARCHAR)
	SECURITY DEFINER
	SET search_path = pg_catalog
	LANGUAGE plpgsql
	AS $$
	DECLARE
		v_requesting_client_id VARCHAR(40);
	BEGIN
		v_requesting_client_id := current_setting('app.current_client_id', true);
		IF v_requesting_client_id IS DISTINCT FROM 'BLACKINK_INTERNAL_SALES' THEN
			-- Fail closed: no rows for any caller outside the one scope this
			-- function exists to serve, rather than trusting the EXECUTE grant
			-- alone (which blackink_app's every tenant session shares).
			RETURN;
		END IF;
		RETURN QUERY
			SELECT c.contact_id, c.company_id FROM public.contacts c WHERE c.email = p_email LIMIT 1;
	END;
	$$
	""",
	# Postgres grants EXECUTE on every new function to PUBLIC by default —
	# revoke it explicitly, matching is_claimed_by_other_client()'s
	# convention. Re-run on every apply so the grant is self-correcting
	# regardless of what a prior version of this migration left in place.
	"REVOKE EXECUTE ON FUNCTION resolve_sales_demo_target(VARCHAR) FROM PUBLIC",
	"GRANT EXECUTE ON FUNCTION resolve_sales_demo_target(VARCHAR) TO blackink_app",
	# CREATE OR REPLACE FUNCTION does not transfer ownership if the function
	# already exists — only the first CREATE sets the owner, and SECURITY
	# DEFINER only bypasses RLS if the owner does. CURRENT_USER, not a
	# hardcoded role name — matches is_claimed_by_other_client()'s rationale.
	"ALTER FUNCTION resolve_sales_demo_target(VARCHAR) OWNER TO CURRENT_USER",

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
