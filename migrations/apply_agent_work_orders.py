"""
Provision agent_work_orders — the durable row behind every Slack action
card (Dev 3 plan §3.3). Runs after apply_clients.py (FK target) and before
apply_rls_policies.py (this table must be tenant-scoped before that
migration's fail-loud verification runs) — see CLAUDE.md's ordered
migration list.

*** action_id is APPLICATION-GENERATED (uuid4 in src/services/work_orders,
NOT the DEFAULT below) — same class of rule as companies.company_id. ***
action_id is part of the payload-hash preimage (src/services/slack/
payload_hash.py:_preimage), so the digest cannot be computed until
action_id is known; if the database assigned it via DEFAULT, enqueue()
would have to INSERT before it could hash, and hash before it could INSERT
— circular. The DEFAULT below exists only as a safety net for a manual/
direct insert, never relied on by application code.

*** client_id references clients(client_id), NOT companies(company_id). ***
The blueprint's own raw SQL (§5.3) has `client_id UUID NOT NULL REFERENCES
companies(company_id)` — wrong on its face, a client_id is not a company
id, and Blackink's landed schema types client_id as VARCHAR(40) everywhere
else (see events.client_id in src/core/models.py). Same class of
blueprint-SQL self-contradiction CLAUDE.md already documents for
companies.company_id (blueprint prose says SHA-256 hash, blueprint SQL
says gen_random_uuid()).

idempotency_key and config_fingerprint/hash_version are NOT in the
blueprint's §5.3 sample DDL — added here because the Idempotency Engine
row the blueprint's own §5.1 table mandates ("every outbound send... update
requires a unique idempotency key") has nothing to attach to without it,
and the hash design (Dev 3 plan §5.2) requires config_fingerprint to exist
as a real column, not an implicit assumption.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_agent_work_orders.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS agent_work_orders (
		action_id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
		client_id            VARCHAR(40)   NOT NULL REFERENCES clients(client_id),
		entity_type          VARCHAR(30)   NOT NULL,
		entity_id            VARCHAR(64)   NOT NULL,
		opportunity_id       VARCHAR(64),
		agent_id             VARCHAR(50)   NOT NULL,
		action_class         VARCHAR(100)  NOT NULL,
		autonomy_band        VARCHAR(20)   NOT NULL,
		risk_class           VARCHAR(20)   NOT NULL,
		confidence_score     NUMERIC(5,2),
		recipient            TEXT,
		payload              JSONB         NOT NULL DEFAULT '{}'::jsonb,
		config_fingerprint   JSONB         NOT NULL DEFAULT '{}'::jsonb,
		payload_hash         VARCHAR(64)   NOT NULL,
		hash_version         SMALLINT      NOT NULL DEFAULT 1,
		status               VARCHAR(20)   NOT NULL DEFAULT 'QUEUED',
		idempotency_key      VARCHAR(160)  NOT NULL,
		slack_channel_id     VARCHAR(30),
		slack_message_ts     VARCHAR(30),
		decided_by           VARCHAR(120),
		decided_at           TIMESTAMPTZ,
		execution_receipt    JSONB,
		error                TEXT,
		due_at               TIMESTAMPTZ,
		created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		updated_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_agent_work_orders_idem UNIQUE (client_id, idempotency_key),
		CONSTRAINT ck_agent_work_orders_status CHECK (status IN
			('QUEUED','APPROVED','REJECTED','SNOOZED','SKIPPED','DONE','EXECUTING','FAILED','CANCELLED')),
		CONSTRAINT ck_agent_work_orders_band CHECK (autonomy_band IN
			('BAND_1_OBSERVE','BAND_2_ONE_TAP','BAND_3_AUTO')),
		CONSTRAINT ck_agent_work_orders_risk CHECK (risk_class IN
			('LOW','MEDIUM','HIGH','CRITICAL'))
	)
	""",
	# Idempotent status-constraint refresh for DBs created before CANCELLED was
	# added (the Mark Opt-Out halt sets status='CANCELLED' — sequence_halt.py).
	"ALTER TABLE agent_work_orders DROP CONSTRAINT IF EXISTS ck_agent_work_orders_status",
	"""
	ALTER TABLE agent_work_orders ADD CONSTRAINT ck_agent_work_orders_status
		CHECK (status IN ('QUEUED','APPROVED','REJECTED','SNOOZED','SKIPPED','DONE','EXECUTING','FAILED','CANCELLED'))
	""",
	"CREATE INDEX IF NOT EXISTS ix_awo_client_status ON agent_work_orders (client_id, status)",
	"CREATE INDEX IF NOT EXISTS ix_awo_entity        ON agent_work_orders (entity_type, entity_id)",
	"CREATE INDEX IF NOT EXISTS ix_awo_due            ON agent_work_orders (status, due_at)",
	"GRANT SELECT, INSERT, UPDATE ON agent_work_orders TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON agent_work_orders TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
		cols = db.execute(
			text(
				"SELECT column_name FROM information_schema.columns "
				"WHERE table_name = 'agent_work_orders' ORDER BY ordinal_position"
			)
		).fetchall()
	print("apply_agent_work_orders: done —", [c.column_name for c in cols])
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
