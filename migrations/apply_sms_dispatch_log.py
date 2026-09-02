"""
Provision the sms_dispatch_log table (Dev 1 plan, Week 1 Subtask 1.2.3 —
Cold SMS Hard Block).

DB-layer backstop of the three-layer cold-SMS block (master blueprint
§3.1.2: "Cold outbound SMS is blocked at the database, application, and
CI/CD testing levels"). The CHECK constraint below enforces the same
literal predicate as src/services/campaign_readiness_gate.py's
_is_engaged() (§3.0.4: inbound_sms_count == 0 AND booked_appointment_id
IS NULL blocks SMS) directly at the database engine — a raw SQL INSERT
for a cold contact is rejected regardless of what application code does,
independent of the application-layer linter in src/services/sms_dispatch.py.

Neither the master blueprint nor the DoD defines a schema for "an
outbound SMS record" — no SMS/message table or Twilio integration exists
anywhere in this repo yet (no vendor contracted, same situation as the
DNC vendor being a stub). This is a minimal table invented to give the
DB-layer constraint something to attach to; SMS sending itself stays a
stub (SmsProvider/StubSmsProvider).

idempotency_key + the PENDING/FAILED status values (PR #10 review fixup)
support src/services/sms_dispatch.py's transactional-outbox dispatch: a
PENDING row with a unique idempotency_key is committed BEFORE the
provider is ever called, so a crash or rollback after the provider
accepted the message still leaves a durable record a reconciliation job
can use instead of blindly retrying (which would double-send). UPDATE is
granted to blackink_app so that same row can move PENDING -> SENT/FAILED
after the provider call, in a transaction of its own.

Idempotent: CREATE TABLE IF NOT EXISTS, self-correcting ADD COLUMN /
DROP+ADD CONSTRAINT for the idempotency_key and status changes.

    PYTHONPATH=. python migrations/apply_sms_dispatch_log.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS sms_dispatch_log (
		dispatch_id                    BIGSERIAL    PRIMARY KEY,
		client_id                      VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		contact_id                     BIGINT       NOT NULL REFERENCES contacts(contact_id),
		inbound_sms_count_at_send      INTEGER      NOT NULL,
		booked_appointment_id_at_send  VARCHAR(64),
		status                         VARCHAR(20)  NOT NULL DEFAULT 'SENT',
		provider_message_id            VARCHAR(100),
		created_at                     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		-- The literal cold-SMS predicate (master blueprint §3.0.4), enforced as
		-- a hard DB constraint independent of dispatch_sms()'s own linter.
		CONSTRAINT ck_sms_dispatch_log_not_cold CHECK (
			inbound_sms_count_at_send > 0 OR booked_appointment_id_at_send IS NOT NULL
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_sms_dispatch_log_contact ON sms_dispatch_log (contact_id, created_at)",
	"CREATE INDEX IF NOT EXISTS ix_sms_dispatch_log_client ON sms_dispatch_log (client_id, created_at)",
	# PENDING/FAILED support the transactional-outbox flow (see module
	# docstring). CREATE TABLE IF NOT EXISTS above won't retrofit this on
	# an already-existing table, so it's a self-correcting DROP+ADD, same
	# pattern as apply_contacts.py's FK-constraint fixup — re-run on every
	# apply regardless of what a prior version left in place.
	"ALTER TABLE sms_dispatch_log DROP CONSTRAINT IF EXISTS ck_sms_dispatch_log_status",
	"""
	ALTER TABLE sms_dispatch_log
		ADD CONSTRAINT ck_sms_dispatch_log_status
		CHECK (status IN ('PENDING', 'SENT', 'FAILED', 'BLOCKED'))
	""",
	# NULL-able + a plain UNIQUE constraint (Postgres allows any number of
	# NULLs under UNIQUE) rather than NOT NULL, so this stays idempotent
	# against a table that already has BLOCKED rows from before this
	# column existed.
	"ALTER TABLE sms_dispatch_log ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)",
	"ALTER TABLE sms_dispatch_log DROP CONSTRAINT IF EXISTS uq_sms_dispatch_log_idempotency_key",
	"""
	ALTER TABLE sms_dispatch_log
		ADD CONSTRAINT uq_sms_dispatch_log_idempotency_key UNIQUE (idempotency_key)
	""",
	"GRANT SELECT, INSERT, UPDATE ON sms_dispatch_log TO blackink_app",
	"GRANT USAGE ON SEQUENCE sms_dispatch_log_dispatch_id_seq TO blackink_app",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_sms_dispatch_log: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
