"""
Provision clients, county_allocations, and client_pm_books (Dev 1 plan,
migration 3 of 11).

Two distinct ownership/suppression mechanisms — see src/core/models.py
docstrings and the Dev 1 plan's "Key architectural decisions" §2 for the
full rationale:

  county_allocations — prospect-pool outreach-rights assignment. Exactly one
                        non-superseded row per county at a time (enforced by
                        a partial unique index, not just convention), with a
                        30-day reassessment window. Adapted from the
                        blueprint's Metro Allocation Algorithm (county-scoped
                        per the client's correction), NOT the removed seat
                        SKUs (seat_a_door_gen etc. stay disabled catalog rows
                        elsewhere, not built here).

  client_pm_books    — a client's real, PMS-synced managed portfolio. This is
                        what the PERMANENT non-poach check queries. No
                        expiry/TTL.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_clients.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS clients (
		client_id           VARCHAR(40)  PRIMARY KEY,
		display_name        VARCHAR(200) NOT NULL,
		is_active            BOOLEAN      NOT NULL DEFAULT TRUE,
		plan_tier            VARCHAR(20)  NOT NULL DEFAULT 'standard',
		contract_start_date  DATE,
		contract_end_date    DATE,
		daily_send_ceiling   INTEGER      NOT NULL DEFAULT 0,
		suspended_at         TIMESTAMPTZ,
		created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
	)
	""",
	"GRANT SELECT, INSERT, UPDATE ON clients TO blackink_app",
	"""
	CREATE TABLE IF NOT EXISTS county_allocations (
		id                BIGSERIAL    PRIMARY KEY,
		county_slug       VARCHAR(60)  NOT NULL REFERENCES counties(county_slug),
		client_id         VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		allocated_at      TIMESTAMPTZ  NOT NULL,
		reassess_after    TIMESTAMPTZ  NOT NULL,
		allocation_reason TEXT,
		superseded_at     TIMESTAMPTZ
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_county_allocations_county ON county_allocations (county_slug)",
	"CREATE INDEX IF NOT EXISTS ix_county_allocations_client ON county_allocations (client_id)",
	# Exactly one active (non-superseded) allocation per county at a time —
	# the actual exclusivity enforcement, not just application convention.
	"""
	CREATE UNIQUE INDEX IF NOT EXISTS uq_county_allocations_active_county
		ON county_allocations (county_slug) WHERE superseded_at IS NULL
	""",
	"GRANT SELECT, INSERT, UPDATE ON county_allocations TO blackink_app",
	"GRANT USAGE ON SEQUENCE county_allocations_id_seq TO blackink_app",
	"""
	CREATE TABLE IF NOT EXISTS client_pm_books (
		id           BIGSERIAL    PRIMARY KEY,
		client_id    VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		owner_domain VARCHAR(255),
		owner_email  VARCHAR(255),
		synced_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_client_pm_books_has_identifier
			CHECK (owner_domain IS NOT NULL OR owner_email IS NOT NULL)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_client_pm_books_client ON client_pm_books (client_id)",
	"CREATE INDEX IF NOT EXISTS ix_client_pm_books_domain ON client_pm_books (owner_domain)",
	"CREATE INDEX IF NOT EXISTS ix_client_pm_books_email ON client_pm_books (owner_email)",
	"GRANT SELECT, INSERT, UPDATE, DELETE ON client_pm_books TO blackink_app",
	"GRANT USAGE ON SEQUENCE client_pm_books_id_seq TO blackink_app",
]

# Reserved internal client_id for Blackink's own platform-level activity
# (self-marketing, system/promotion-sweep events not yet tied to a paying
# client) — mirrors the blueprint's own events schema comment: "Blackink
# self-marketing uses dedicated internal client_id." is_active=TRUE so it
# resolves normally through get_client_config(), but it is never a real
# customer and must never be assigned county allocations.
SEED_INTERNAL_CLIENT_SQL = """
	INSERT INTO clients (client_id, display_name, is_active, plan_tier)
	VALUES ('_platform_internal', 'Blackink Internal / Self-Marketing', TRUE, 'internal')
	ON CONFLICT (client_id) DO NOTHING
"""


def main() -> int:
	with get_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.execute(text(SEED_INTERNAL_CLIENT_SQL))
		db.commit()
		cols = db.execute(
			text(
				"SELECT table_name, column_name FROM information_schema.columns "
				"WHERE table_name IN ('clients','county_allocations','client_pm_books') "
				"ORDER BY table_name, ordinal_position"
			)
		).fetchall()
	print(f"apply_clients: done — {len(cols)} columns across clients/county_allocations/client_pm_books")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
