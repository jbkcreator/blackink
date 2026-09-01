"""
Provision the events table — the shared, client_id-scoped, append-only
ledger every other subsystem reads from and writes to (Dev 1 plan,
migration 8 of 12).

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_events.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS events (
		id           BIGSERIAL    PRIMARY KEY,
		client_id    VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		event_type   VARCHAR(60)  NOT NULL,
		entity_type  VARCHAR(30)  NOT NULL,
		entity_id    VARCHAR(64)  NOT NULL,
		payload      JSONB        NOT NULL DEFAULT '{}'::jsonb,
		actor        VARCHAR(100),
		created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_events_client_created ON events (client_id, created_at)",
	"CREATE INDEX IF NOT EXISTS ix_events_entity ON events (entity_type, entity_id)",
	"GRANT SELECT, INSERT ON events TO blackink_app",
	"GRANT SELECT, INSERT ON events TO blackink_system",
	"GRANT USAGE ON SEQUENCE events_id_seq TO blackink_app, blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_events: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
