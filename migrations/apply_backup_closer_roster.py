"""
Provision backup_closer_roster (S-13, W2 §3.2.1 — SLA tier-3 reallocation).

respond_sla_sweep.py's tier-3 escalation (240 minutes unclaimed) previously
only posted "assign to backup closer queue manually" with no real roster or
assignment target behind it. This table is each PAYING CLIENT's own pool of
backup closers (their real, client-side sales reps who can take over an
unclaimed lead) — a direct client_id column, same reasoning as
meeting_outcome_prompt_jobs/winback_rows in config/tenant_policies.py's
comments: this is per-tenant operational roster data, not global reference
config, so it's registered in TENANT_POLICIES and pushed through
apply_rls_policies.py, unlike Subtask 2.1.3's knowledge_base_entries
(migrations/apply_knowledge_base.py), which is global reference config.

last_assigned_at drives round-robin (least-recently-assigned next) —
src/tasks/respond_sla_sweep.py's tier-3 claims the oldest-assigned active
row with SELECT ... FOR UPDATE SKIP LOCKED so two concurrent sweep ticks
never assign the same roster slot to two different leads at once.

Also adds the reallocation columns onto inbound_messages
(assigned_closer_slack_user_id, reallocated_at) in the same migration.

Idempotent: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_backup_closer_roster.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS backup_closer_roster (
		id                BIGSERIAL     PRIMARY KEY,
		client_id         VARCHAR(40)   NOT NULL REFERENCES clients(client_id),
		slack_user_id     VARCHAR(20)   NOT NULL,
		display_name      VARCHAR(200),
		is_active         BOOLEAN       NOT NULL DEFAULT TRUE,
		last_assigned_at  TIMESTAMPTZ,
		created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
		CONSTRAINT uq_backup_closer_roster_client_user UNIQUE (client_id, slack_user_id)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_backup_closer_roster_pick ON backup_closer_roster "
	"(client_id, last_assigned_at) WHERE is_active = TRUE",
	"REVOKE DELETE ON backup_closer_roster FROM blackink_app",
	"GRANT SELECT ON backup_closer_roster TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON backup_closer_roster TO blackink_system",
	"GRANT USAGE ON SEQUENCE backup_closer_roster_id_seq TO blackink_system",
	# Tier-3 reallocation target + audit timestamp.
	"ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS assigned_closer_slack_user_id VARCHAR(20)",
	"ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS reallocated_at TIMESTAMPTZ",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_backup_closer_roster: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
