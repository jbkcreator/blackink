"""
Provision owner_entities / owner_entity_links, and add companies.owner_entity_id
(Dev 1 plan, migration 6 of 12).

Schema scaffolding ONLY — not populated by Dev 1. Structural analog of
Forced Action's BuyerEntity/BuyerEntityLink. Additive-only: adding this now
means a future entity-resolution job (full LLC beneficial-owner piercing,
the Owner Score formula — neither exists anywhere yet) won't need a
breaking migration. See Dev 1 plan's "Entity resolution — scope boundary".

Idempotent: CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_owner_entities.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS owner_entities (
		id                        BIGSERIAL   PRIMARY KEY,
		canonical_name            TEXT        NOT NULL,
		entity_type               VARCHAR(20) NOT NULL,
		portfolio_door_count_est  INTEGER,
		confidence_score          INTEGER     NOT NULL DEFAULT 0,
		verification_status       VARCHAR(20) NOT NULL DEFAULT 'unverified',
		created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
		updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_owner_entities_type CHECK (
			entity_type IN ('INDIVIDUAL','LLC','TRUST','CORPORATE','REIT')
		)
	)
	""",
	"GRANT SELECT, INSERT, UPDATE ON owner_entities TO blackink_app",
	"GRANT USAGE ON SEQUENCE owner_entities_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON owner_entities TO blackink_system",
	"GRANT USAGE ON SEQUENCE owner_entities_id_seq TO blackink_system",
	"""
	CREATE TABLE IF NOT EXISTS owner_entity_links (
		id                BIGSERIAL   PRIMARY KEY,
		owner_entity_id   BIGINT      NOT NULL REFERENCES owner_entities(id) ON DELETE CASCADE,
		source_table      VARCHAR(30) NOT NULL,
		source_id         VARCHAR(64) NOT NULL,
		match_confidence  INTEGER     NOT NULL DEFAULT 0,
		match_method      VARCHAR(30) NOT NULL,
		linked_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_owner_entity_links_source CHECK (source_table IN ('companies','contacts')),
		CONSTRAINT uq_owner_entity_link_source UNIQUE (source_table, source_id)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_owner_entity_links_entity ON owner_entity_links (owner_entity_id)",
	"GRANT SELECT, INSERT, UPDATE, DELETE ON owner_entity_links TO blackink_app",
	"GRANT USAGE ON SEQUENCE owner_entity_links_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE, DELETE ON owner_entity_links TO blackink_system",
	"GRANT USAGE ON SEQUENCE owner_entity_links_id_seq TO blackink_system",
	"ALTER TABLE companies ADD COLUMN IF NOT EXISTS owner_entity_id BIGINT REFERENCES owner_entities(id)",
	"CREATE INDEX IF NOT EXISTS ix_companies_owner_entity ON companies (owner_entity_id)",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_owner_entities: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
