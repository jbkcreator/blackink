"""
Provision the appointment-operations schema (Subtask 1.1.1 — Deploy
Appointment Tables & State Enum).

Blueprint source: Source D p5 (`009_appointment_ops.sql`). The settlement
billing gate (lands Week 4) depends on these tables; this subtask establishes
the schema and state machine so settlement can issue invoices correctly.
`appointments.is_billable` is a STORED generated column that is TRUE only when
state = 'ATTENDED' AND both confirmation timestamps are non-null — the
schema-level slice of the qualification bar; the full 4-rule ownership/intent/
ICP/duration check lands separately and remains the billing truth (Source D's
own caveats, p-block lines 457-459).

── Deliberate adaptation of the printed DDL to this repository's real schema ──
The blueprint DDL was written against a schema where company_id/contact_id are
UUIDs and `client_id` is a UUID FK to companies(company_id). None of that is
true here and it would not migrate:
  * companies.company_id is VARCHAR(64) (sha256 of the normalized domain — see
    apply_companies.py), never a UUID.
  * contacts.contact_id is BIGSERIAL.
  * `client_id` in this codebase is the VARCHAR(40) paying-tenant key on
    clients(client_id) and is the Row-Level-Security boundary — it is NOT the
    prospect company.
So the printed `client_id UUID REFERENCES companies` is split into two real
columns, honoring both the blueprint's intent and this repo's non-negotiable
tenant-isolation invariant:
  * client_id  VARCHAR(40) NOT NULL REFERENCES clients      — paying tenant / RLS boundary
  * company_id VARCHAR(64)          REFERENCES companies    — company the appointment is with
  * contact_id BIGINT               REFERENCES contacts
  * opportunity_id UUID NOT NULL — preserved across reschedules (blueprint intent, kept).
All four appointment tables carry their own client_id and are registered in
config/tenant_policies.py as {"mode": "direct", "column": "client_id"}, so
apply_rls_policies.py FORCEs RLS on every one of them and
tests/test_tenant_isolation.py probes them — none is left unscoped (per the
subtask's tenant-isolation requirement). Child tables carry client_id directly
rather than relying on a parent join, so a mis-scoped INSERT is rejected at the
row it is written on, not one hop away.

idx_opportunity_dedupe is intentionally a NON-unique index (as the blueprint
specifies, and as Source D's caveat notes it is not itself the exactly-once
financial constraint): a single opportunity legitimately spans multiple
appointment rows across reschedules / no-show recovery / rebooking. Billing
idempotency at the opportunity level is enforced by the settlement layer, not a
unique index here.

── Same-tenant referential integrity (PR #25 review fix) ──
A bare `appointment_id UUID REFERENCES appointments(appointment_id)` on a
child table only proves the referenced appointment exists — not that it
belongs to the SAME client_id as the child row. Under RLS, a tenant-scoped
writer for client B can construct a confirmation_logs/appointment_dispositions/
appointment_disputes row carrying client_id='B' that references an
appointment_id it looked up (or is handed) belonging to client A, since the FK
constraint alone never inspects the appointment's client_id. RLS on the child
table only ever checks the child row's OWN client_id, not the parent's.
Fixed here by adding `UNIQUE (client_id, appointment_id)` on appointments and
replacing every child FK with a COMPOSITE `FOREIGN KEY (client_id,
appointment_id) REFERENCES appointments(client_id, appointment_id)` — a
mismatched (client_id, appointment_id) pair is now rejected by Postgres itself,
before RLS is even evaluated. See tests/test_tenant_isolation.py for the live
proof that a Tenant-B session cannot insert a child row against a Tenant-A
appointment_id.

State-machine rules ("reschedule capped at 2 → LOST", "opportunity_id retained
across reschedules and no-show recovery") are enforced in the application layer
— see src/services/appointment_state.py — since a computed generated column
cannot express a transition guard. A BEFORE UPDATE trigger
(`trg_appointments_guard_transition` / `appointments_guard_transition()`) is
the enforcement backstop (PR #25 review fix): it rejects
`reschedule_count > 2`, rejects `reschedule_count` decreasing, and rejects any
change to `opportunity_id` — the two invariants the module docstring calls
"hard" but that, before this trigger, only held if every caller remembered to
route through appointment_state.py. Direct UPDATE access on the table stays
(needed for state/timestamp columns the trigger does not touch), but the two
billing-critical invariants can no longer be bypassed by a stray hand-written
UPDATE.

Idempotent: CREATE TYPE guarded by a catalog check, everything else
CREATE ... IF NOT EXISTS. Run BEFORE apply_rls_policies.py, AFTER apply_clients
/ apply_companies / apply_contacts.

    PYTHONPATH=. python migrations/apply_appointment_ops.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	# 1. APPOINTMENT STATE ENUM — CREATE TYPE has no IF NOT EXISTS, so guard on
	#    the catalog to stay idempotent.
	"""
	DO $$
	BEGIN
		IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'appointment_state_enum') THEN
			CREATE TYPE appointment_state_enum AS ENUM (
				'BOOKED', 'CONFIRMED_24H', 'CONFIRMED_3H', 'ATTENDED', 'DISPOSITIONED',
				'RESCHEDULED', 'NO_SHOW_RECOVERY', 'REBOOKED', 'LOST'
			);
		END IF;
	END$$;
	""",
	# 2. APPOINTMENT TRACKING MASTER TABLE
	"""
	CREATE TABLE IF NOT EXISTS appointments (
		appointment_id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id                VARCHAR(40)   NOT NULL REFERENCES clients(client_id),
		company_id               VARCHAR(64)   REFERENCES companies(company_id),
		opportunity_id           UUID          NOT NULL,
		contact_id               BIGINT        REFERENCES contacts(contact_id),
		state                    appointment_state_enum NOT NULL DEFAULT 'BOOKED',
		reschedule_count         INTEGER       NOT NULL DEFAULT 0,
		scheduled_for            TIMESTAMPTZ   NOT NULL,
		attended_at              TIMESTAMPTZ,
		confirmed_24h_timestamp  TIMESTAMPTZ,
		confirmed_3h_timestamp   TIMESTAMPTZ,
		is_billable              BOOLEAN       GENERATED ALWAYS AS (
			state = 'ATTENDED' AND
			confirmed_24h_timestamp IS NOT NULL AND
			confirmed_3h_timestamp IS NOT NULL
		) STORED,
		owner_brief_url          TEXT          NOT NULL,
		created_at               TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		updated_at               TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		-- Lets every child table FK on (client_id, appointment_id) instead of
		-- appointment_id alone, so a mismatched tenant/appointment pair is
		-- rejected by the FK constraint itself (PR #25 review fix).
		UNIQUE (client_id, appointment_id)
	)
	""",
	# 3. CONFIRMATION AUDIT LOG
	"""
	CREATE TABLE IF NOT EXISTS confirmation_logs (
		log_id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id          VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		appointment_id     UUID         NOT NULL,
		channel            VARCHAR(10)  NOT NULL CHECK (channel IN ('SMS', 'EMAIL')),
		confirmation_tier  VARCHAR(10)  NOT NULL CHECK (confirmation_tier IN ('24H', '3H')),
		sent_at            TIMESTAMPTZ  NOT NULL,
		delivery_status    VARCHAR(50)  NOT NULL,
		reply_received_at  TIMESTAMPTZ,
		raw_response       TEXT,
		created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT fk_confirmation_logs_appointment_same_tenant
			FOREIGN KEY (client_id, appointment_id)
			REFERENCES appointments (client_id, appointment_id)
			ON DELETE CASCADE
	)
	""",
	# 4. DISPOSITION CAPTURE TABLE
	#    outcome is the blueprint's four values. Sept-04 triage Item 22
	#    ("sell_intent Disposition", Week 3) will later add a fifth value to this
	#    CHECK — deliberately NOT added here (out of scope for the 1.1.1 schema
	#    deploy). The constraint is named (ck_appt_disposition_outcome) so that
	#    Week-3 change is a clean DROP/ADD CONSTRAINT, not a table rebuild.
	"""
	CREATE TABLE IF NOT EXISTS appointment_dispositions (
		disposition_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id               VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		appointment_id          UUID  UNIQUE NOT NULL,
		outcome                 VARCHAR(20)  NOT NULL CHECK (outcome IN ('SIGNED', 'DECIDING', 'NO', 'NOT_A_FIT')),
		doors_signed            INTEGER      NOT NULL DEFAULT 0,
		close_reason            VARCHAR(50)  CHECK (close_reason IN (
			'PRICE', 'TIMING', 'STAYING_SELF_MANAGED', 'WENT_ELSEWHERE', 'NOT_QUALIFIED'
		)),
		brief_accurate          VARCHAR(10)  NOT NULL CHECK (brief_accurate IN ('YES', 'PARTLY', 'NO')),
		disposition_captured_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT fk_appointment_dispositions_appointment_same_tenant
			FOREIGN KEY (client_id, appointment_id)
			REFERENCES appointments (client_id, appointment_id)
			ON DELETE CASCADE
	)
	""",
	# 5. DISPUTE AUDIT LOG
	#    Sept-04 triage Item 10 ("Dispute Credited on Flagging, Not on
	#    Resolution", Week 2) governs this table: the credit is written at
	#    flagged_at, the dispute window is 48h from the meeting's scheduled_at,
	#    and outcome defaults to CREDITED_AUTOMATIC. That credit-on-flag rule is
	#    why resolved_at is left NULLable with NO default here — the printed
	#    blueprint DDL had `resolved_at DEFAULT CURRENT_TIMESTAMP`, which would
	#    stamp a resolution time at creation and contradict Item 10. The 48h
	#    window and the credit-line write are Week-2 billing logic (not this
	#    schema subtask); the columns to support them exist here.
	"""
	CREATE TABLE IF NOT EXISTS appointment_disputes (
		dispute_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id       VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		appointment_id  UUID  UNIQUE NOT NULL,
		flagged_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		reason          TEXT         NOT NULL,
		evidence_ref    TEXT,
		outcome         VARCHAR(50)  NOT NULL DEFAULT 'CREDITED_AUTOMATIC',
		resolved_at     TIMESTAMPTZ,
		CONSTRAINT fk_appointment_disputes_appointment_same_tenant
			FOREIGN KEY (client_id, appointment_id)
			REFERENCES appointments (client_id, appointment_id)
			ON DELETE CASCADE
	)
	""",
	# 2a. SELF-CORRECTING UPGRADE for an already-applied instance of this
	#     migration (pre-PR-#25-fix): add the composite UNIQUE on appointments
	#     and swap every child FK from single-column appointment_id to the
	#     composite (client_id, appointment_id) pair. Re-run on every apply, same
	#     self-correcting pattern as apply_contacts.py's company_id FK fixup —
	#     harmless once already applied.
	# Postgres has no `ADD CONSTRAINT IF NOT EXISTS` (only DROP CONSTRAINT
	# supports IF EXISTS), so each addition is guarded via pg_constraint.
	"""
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conname = 'appointments_client_id_appointment_id_key'
		) THEN
			ALTER TABLE appointments ADD CONSTRAINT appointments_client_id_appointment_id_key UNIQUE (client_id, appointment_id);
		END IF;
	END$$;
	""",
	"ALTER TABLE confirmation_logs DROP CONSTRAINT IF EXISTS confirmation_logs_appointment_id_fkey",
	"""
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conname = 'fk_confirmation_logs_appointment_same_tenant'
		) THEN
			ALTER TABLE confirmation_logs
				ADD CONSTRAINT fk_confirmation_logs_appointment_same_tenant
				FOREIGN KEY (client_id, appointment_id)
				REFERENCES appointments (client_id, appointment_id)
				ON DELETE CASCADE;
		END IF;
	END$$;
	""",
	"ALTER TABLE appointment_dispositions DROP CONSTRAINT IF EXISTS appointment_dispositions_appointment_id_fkey",
	"""
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conname = 'fk_appointment_dispositions_appointment_same_tenant'
		) THEN
			ALTER TABLE appointment_dispositions
				ADD CONSTRAINT fk_appointment_dispositions_appointment_same_tenant
				FOREIGN KEY (client_id, appointment_id)
				REFERENCES appointments (client_id, appointment_id)
				ON DELETE CASCADE;
		END IF;
	END$$;
	""",
	"ALTER TABLE appointment_disputes DROP CONSTRAINT IF EXISTS appointment_disputes_appointment_id_fkey",
	"""
	DO $$
	BEGIN
		IF NOT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conname = 'fk_appointment_disputes_appointment_same_tenant'
		) THEN
			ALTER TABLE appointment_disputes
				ADD CONSTRAINT fk_appointment_disputes_appointment_same_tenant
				FOREIGN KEY (client_id, appointment_id)
				REFERENCES appointments (client_id, appointment_id)
				ON DELETE CASCADE;
		END IF;
	END$$;
	""",
	# 6a. TRANSITION-GUARD + SAME-TENANT-OWNERSHIP TRIGGER (PR #25 review fix).
	#     A single BEFORE INSERT OR UPDATE trigger, since Postgres has no
	#     composite-FK equivalent for company_id/contact_id — companies is a
	#     shared prospect pool (owning_client_id reassigned by
	#     county_allocation_reassessment.py), so a static composite FK would
	#     break the moment ownership legitimately moves. Checked at write time
	#     instead, against the live owning_client_id.
	"""
	CREATE OR REPLACE FUNCTION appointments_guard_transition() RETURNS TRIGGER AS $$
	DECLARE
		v_owning_client_id VARCHAR(40);
	BEGIN
		IF TG_OP = 'UPDATE' THEN
			IF NEW.opportunity_id IS DISTINCT FROM OLD.opportunity_id THEN
				RAISE EXCEPTION 'opportunity_id is immutable once set (billing anchor, appointment %)', OLD.appointment_id;
			END IF;
			IF NEW.reschedule_count < OLD.reschedule_count THEN
				RAISE EXCEPTION 'reschedule_count cannot decrease (appointment %)', OLD.appointment_id;
			END IF;
		END IF;
		IF NEW.reschedule_count > 2 THEN
			RAISE EXCEPTION 'reschedule_count cannot exceed 2 — a third reschedule must set state to LOST instead (appointment %)', NEW.appointment_id;
		END IF;

		IF NEW.company_id IS NOT NULL THEN
			SELECT owning_client_id INTO v_owning_client_id
			FROM companies WHERE company_id = NEW.company_id;
			IF v_owning_client_id IS DISTINCT FROM NEW.client_id THEN
				RAISE EXCEPTION 'company % is not owned by client % (owning_client_id=%)',
					NEW.company_id, NEW.client_id, v_owning_client_id;
			END IF;
		END IF;

		IF NEW.contact_id IS NOT NULL THEN
			SELECT c.owning_client_id INTO v_owning_client_id
			FROM contacts ct JOIN companies c ON c.company_id = ct.company_id
			WHERE ct.contact_id = NEW.contact_id;
			IF v_owning_client_id IS DISTINCT FROM NEW.client_id THEN
				RAISE EXCEPTION 'contact % is not owned by client % (owning_client_id=%)',
					NEW.contact_id, NEW.client_id, v_owning_client_id;
			END IF;
		END IF;

		RETURN NEW;
	END;
	$$ LANGUAGE plpgsql;
	""",
	"""
	DROP TRIGGER IF EXISTS trg_appointments_guard_transition ON appointments;
	""",
	"""
	CREATE TRIGGER trg_appointments_guard_transition
		BEFORE INSERT OR UPDATE ON appointments
		FOR EACH ROW EXECUTE FUNCTION appointments_guard_transition();
	""",
	# 6. INDICES — the billing-gate composite (index scan on the settlement
	#    query) and the opportunity-dedupe lookup (non-unique, see module docstring).
	"CREATE INDEX IF NOT EXISTS idx_appointments_billing_gate ON appointments(state, confirmed_24h_timestamp, confirmed_3h_timestamp)",
	"CREATE INDEX IF NOT EXISTS idx_opportunity_dedupe ON appointments(opportunity_id)",
	# Tenant-column and child-FK lookup support.
	"CREATE INDEX IF NOT EXISTS ix_appointments_client ON appointments(client_id)",
	"CREATE INDEX IF NOT EXISTS ix_confirmation_logs_appointment ON confirmation_logs(appointment_id)",
	# Least-privilege grants — mirrors the sibling job tables. No DELETE for
	# either runtime role: an appointment / audit row is status-transitioned,
	# never removed. appointments uses a UUID PK (gen_random_uuid), so there is
	# no sequence to grant, unlike the BIGSERIAL job tables.
	"REVOKE DELETE ON appointments, confirmation_logs, appointment_dispositions, appointment_disputes FROM blackink_app",
	"REVOKE DELETE ON appointments, confirmation_logs, appointment_dispositions, appointment_disputes FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON appointments, confirmation_logs, appointment_dispositions, appointment_disputes TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON appointments, confirmation_logs, appointment_dispositions, appointment_disputes TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_appointment_ops: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
