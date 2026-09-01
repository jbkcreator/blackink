"""
Provision sending_domains / mailboxes — deliverability tracking for the 20
domains / 40 mailboxes (Dev 1 plan, migration 10 of 12).

DNS/SPF/DKIM/DMARC setup and mailbox warmup are a manual runbook, not code
(Dev 1 plan §Key decision 5 — Forced Action made the identical call in ADR
0011). These tables only track state; src/tasks/deliverability_sentinel.py
consumes it. client_id NULL = Blackink self-marketing (5 of the 20 domains).
NULL vs. non-NULL structurally enforces "never pooled across clients".

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_sending_domains.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS sending_domains (
		id                  BIGSERIAL    PRIMARY KEY,
		domain               VARCHAR(255) NOT NULL UNIQUE,
		client_id            VARCHAR(40)  REFERENCES clients(client_id),
		cluster_label        VARCHAR(100),
		spf_validated        BOOLEAN      NOT NULL DEFAULT FALSE,
		dkim_validated       BOOLEAN      NOT NULL DEFAULT FALSE,
		dmarc_validated      BOOLEAN      NOT NULL DEFAULT FALSE,
		warmup_status        VARCHAR(20)  NOT NULL DEFAULT 'not_started',
		health_score         INTEGER,
		quarantine_state     VARCHAR(20)  NOT NULL DEFAULT 'active',
		quarantined_at       TIMESTAMPTZ,
		quarantine_reason    TEXT,
		is_reserve           BOOLEAN      NOT NULL DEFAULT FALSE,
		created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_sending_domains_warmup_status CHECK (
			warmup_status IN ('not_started','warming','warmed','paused')
		),
		CONSTRAINT ck_sending_domains_quarantine_state CHECK (
			quarantine_state IN ('active','quarantined','reserve')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_sending_domains_client ON sending_domains (client_id)",
	"CREATE INDEX IF NOT EXISTS ix_sending_domains_cluster ON sending_domains (cluster_label)",
	"GRANT SELECT, INSERT, UPDATE ON sending_domains TO blackink_app",
	"GRANT USAGE ON SEQUENCE sending_domains_id_seq TO blackink_app",
	# deliverability_sentinel.py runs as blackink_system and quarantines/
	# swaps domains across every client's cluster.
	"GRANT SELECT, INSERT, UPDATE ON sending_domains TO blackink_system",
	"GRANT USAGE ON SEQUENCE sending_domains_id_seq TO blackink_system",
	"""
	CREATE TABLE IF NOT EXISTS mailboxes (
		id                        BIGSERIAL    PRIMARY KEY,
		domain_id                  BIGINT       NOT NULL REFERENCES sending_domains(id),
		mailbox_address             VARCHAR(255) NOT NULL UNIQUE,
		client_id                   VARCHAR(40)  REFERENCES clients(client_id),
		instantly_account_email     VARCHAR(255),
		warmup_status                VARCHAR(20)  NOT NULL DEFAULT 'not_started',
		health_score                 INTEGER,
		quarantine_state             VARCHAR(20)  NOT NULL DEFAULT 'active',
		created_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_mailboxes_warmup_status CHECK (
			warmup_status IN ('not_started','warming','warmed','paused')
		),
		CONSTRAINT ck_mailboxes_quarantine_state CHECK (
			quarantine_state IN ('active','quarantined','reserve')
		)
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_mailboxes_domain ON mailboxes (domain_id)",
	"CREATE INDEX IF NOT EXISTS ix_mailboxes_client ON mailboxes (client_id)",
	"GRANT SELECT, INSERT, UPDATE ON mailboxes TO blackink_app",
	"GRANT USAGE ON SEQUENCE mailboxes_id_seq TO blackink_app",
	"GRANT SELECT, INSERT, UPDATE ON mailboxes TO blackink_system",
	"GRANT USAGE ON SEQUENCE mailboxes_id_seq TO blackink_system",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_sending_domains: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
