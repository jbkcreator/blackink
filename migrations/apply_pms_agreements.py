"""
Provision pms_agreements (Subtask 1.2.2 — 50/50 Settlement Split Engine &
60-Day Clawback Monitor).

The blueprint's settlement pipeline is triggered by a nightly PMS sync
confirming a signed management agreement (`door_signed`, Tasks/
Project_Blackink_-_Complete_Implementation_Blueprint__Full__v2.md:580,
:1383) and re-verifies that agreement's active status at day 60. Neither
event has ever had a real data source in this repo: no nightly PMS
read-sync exists (apply_clients.py's own docstring calls it out as
"not yet built"), and client_pm_books — the one PMS-adjacent table that
does exist — is populated by nothing.

── Why a new table, not extra columns on client_pm_books ──
client_pm_books is read by is_claimed_by_other_client() (see
apply_compliance_gate_audit.py) as the PERMANENT, no-TTL non-poach lock —
apply_clients.py and config/settings.py both say so explicitly: a company
stays protected for as long as it's in that table, driven by the nightly
PMS sync, never by a timer. Adding signed/terminated lifecycle columns
there would invite a future "AND terminated_at IS NULL" predicate inside
that SECURITY DEFINER function, silently converting a permanent compliance
lock into an expiring one — a compliance regression that would be
invisible in review of a settlement-engine PR. Its grain is also wrong
(one row per owner_domain/owner_email claim, no property reference, no
door count), and it is the one table in this schema granting DELETE to
both runtime roles (sync churn is expected to delete rows there) — a
financial anchor must never sit on a row a sync job may delete.

Integration instead of mutation: src/services/settlement/ledger.py's
record_door_signed() writes the pms_agreements row AND upserts the owner
claim into client_pm_books in the same transaction — a signed agreement
genuinely does establish a permanent non-poach claim. Termination
(record_agreement_terminated()) writes ONLY pms_agreements.terminated_at;
the client_pm_books claim row is left completely untouched, so
is_claimed_by_other_client() semantics are byte-for-byte unchanged. This is
asserted by a live test (see tests/test_pms_agreements_live.py).

opportunity_id is deliberately NOT a foreign key to appointments —
idx_opportunity_dedupe on that table is intentionally non-unique (one
opportunity legitimately spans many appointment rows across reschedules /
no-show recovery), so there is no single appointment row to reference.

agreement_source distinguishes a real nightly-sync row ('PMS_SYNC') from
an operator-entered one ('MANUAL') from the DoD's synthetic test-mode
event ('SYNTHETIC') — src/services/settlement/charge.py's guard trigger
(see apply_settlement_ledger.py) refuses to let a non-PMS_SYNC agreement
back a live (non-test-mode) Stripe charge.

No nightly PMS provider is implemented here — src/services/pms_sync.py's
StubPmsProvider returns None (couldn't determine) for every call. So out
of the box this table is populated by nothing either, same as
client_pm_books today; landing a real PMS integration is a follow-up that
implements the PmsProvider ABC, not a schema change.

Idempotent: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS. Run
AFTER apply_clients.py / apply_companies.py / apply_owner_contacts.py,
BEFORE apply_settlement_ledger.py and apply_rls_policies.py.

    PYTHONPATH=. python migrations/apply_pms_agreements.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS pms_agreements (
		pms_agreement_id  BIGSERIAL    PRIMARY KEY,
		client_id         VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		company_id        VARCHAR(64)  REFERENCES companies(company_id),
		owner_contact_id  BIGINT       REFERENCES owner_contacts(owner_contact_id),
		opportunity_id    UUID         NOT NULL,
		pms_property_ref  TEXT,
		door_count        INTEGER      NOT NULL DEFAULT 1 CHECK (door_count > 0),
		agreement_source  VARCHAR(20)  NOT NULL,
		status            VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
		attempts          INTEGER      NOT NULL DEFAULT 0,
		last_error        TEXT,
		next_retry_at     TIMESTAMPTZ,
		claimed_at        TIMESTAMPTZ,
		door_signed_at    TIMESTAMPTZ  NOT NULL,
		verified_at       TIMESTAMPTZ,
		last_verified_at  TIMESTAMPTZ,
		terminated_at     TIMESTAMPTZ,
		created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_pms_agreements_status CHECK (
			status IN ('ACTIVE', 'TERMINATED', 'UNVERIFIED', 'SUPERSEDED')
		),
		CONSTRAINT ck_pms_agreements_source CHECK (
			agreement_source IN ('PMS_SYNC', 'MANUAL', 'SYNTHETIC')
		),
		CONSTRAINT ck_pms_agreements_terminated CHECK (
			(status = 'TERMINATED') = (terminated_at IS NOT NULL)
		),
		-- Guards against a duplicate ingest of the same signed agreement.
		CONSTRAINT uq_pms_agreements_identity UNIQUE (client_id, opportunity_id, door_signed_at),
		-- Lets settlement_transactions FK on (client_id, pms_agreement_id) so a
		-- mismatched tenant/agreement pair is rejected by the FK constraint
		-- itself — same composite-FK trick as apply_appointment_ops.py.
		CONSTRAINT uq_pms_agreements_tenant_id UNIQUE (client_id, pms_agreement_id)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_pms_agreements_client ON pms_agreements(client_id)",
	"CREATE INDEX IF NOT EXISTS ix_pms_agreements_opportunity ON pms_agreements(client_id, opportunity_id)",
	"CREATE INDEX IF NOT EXISTS ix_pms_agreements_status ON pms_agreements(status, door_signed_at)",
	# Financial anchor — never deletable at runtime, same posture as
	# appointments/settlement_transactions.
	"REVOKE DELETE ON pms_agreements FROM blackink_app",
	"REVOKE DELETE ON pms_agreements FROM blackink_system",
	"GRANT SELECT, INSERT, UPDATE ON pms_agreements TO blackink_app",
	"GRANT USAGE ON SEQUENCE pms_agreements_pms_agreement_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON pms_agreements TO blackink_system",
	"GRANT USAGE ON SEQUENCE pms_agreements_pms_agreement_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_pms_agreements: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
