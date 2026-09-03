"""
Provision calendar_connections + oauth_connect_nonces (Dev 3, Subtask
3.2.1 — Inbound Booking Engine).

One row per client's connected calendar (Google Calendar or Microsoft
Graph — per client comment W1-8, Blackink never books into a
Blackink-owned calendar and Calendly is explicitly out of scope; see
Blackink_Source_of_Truth.md line 577). Resolves an inbound webhook
notification (which carries only a provider subscription/channel id,
not a client_id) back to a tenant, holds the provider-issued
verification secret needed to validate that notification, the encrypted
OAuth tokens (via src/core/token_crypto.py — no per-row DB encryption
existed anywhere in this repo before this table), and the sync-token/
delta-link state that lets an incremental sync tell "already seen" from
"genuinely new" without a race-prone SELECT-then-compare.

oauth_connect_nonces backs the single-use signed connect-link
(mint_calendar_connect_link) that stands in for a full client-portal
login system, which does not exist anywhere in this repo — the nonce
row is what makes the link genuinely single-use (consumed, not just
time-limited).

Idempotent: CREATE TABLE IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_calendar_connections.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"""
	CREATE TABLE IF NOT EXISTS calendar_connections (
		connection_id             BIGSERIAL    PRIMARY KEY,
		client_id                 VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		provider                  VARCHAR(20)  NOT NULL,
		external_calendar_id      VARCHAR(255) NOT NULL,
		subscription_id           VARCHAR(255) NOT NULL,
		verification_secret       VARCHAR(255) NOT NULL,
		access_token_encrypted    TEXT,
		refresh_token_encrypted   TEXT,
		token_expires_at          TIMESTAMPTZ,
		sync_token                TEXT,
		initial_sync_done         BOOLEAN      NOT NULL DEFAULT FALSE,
		status                    VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
		expires_at                TIMESTAMPTZ,
		created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		CONSTRAINT ck_calendar_connections_status CHECK (
			status IN ('ACTIVE', 'NEEDS_RECONNECT', 'DISCONNECTED')
		),
		CONSTRAINT uq_calendar_connections_provider_subscription UNIQUE (provider, subscription_id),
		CONSTRAINT uq_calendar_connections_client_provider UNIQUE (client_id, provider)
	)
	""",
	# Self-correcting DROP+ADD (same pattern as apply_sms_dispatch_log.py) —
	# GOHIGHLEVEL added when the GHL fallback (client comment W1-8) was
	# built; re-run on every apply regardless of what a prior version left.
	"ALTER TABLE calendar_connections DROP CONSTRAINT IF EXISTS ck_calendar_connections_provider",
	"""
	ALTER TABLE calendar_connections
		ADD CONSTRAINT ck_calendar_connections_provider CHECK (provider IN ('GOOGLE', 'MICROSOFT', 'GOHIGHLEVEL'))
	""",
	# GHL has no OAuth flow and no separate calendar-id concept (see
	# src/services/ghl_webhook.py) — the column stays NOT NULL for
	# Google/Microsoft rows; GHL rows store a fixed placeholder.
	"ALTER TABLE calendar_connections ALTER COLUMN external_calendar_id DROP NOT NULL",
	"CREATE INDEX IF NOT EXISTS ix_calendar_connections_client ON calendar_connections (client_id)",
	# DELETE granted alongside SELECT/INSERT/UPDATE — same convention as
	# contacts/companies, needed for test-fixture teardown, not just
	# app-role runtime writes.
	"GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_connections TO blackink_app",
	"GRANT USAGE ON SEQUENCE calendar_connections_connection_id_seq TO blackink_app",
	# calendar_subscription_renewal.py / calendar_sync_worker.py run as blackink_system.
	"GRANT SELECT, INSERT, UPDATE, DELETE ON calendar_connections TO blackink_system",
	"GRANT USAGE ON SEQUENCE calendar_connections_connection_id_seq TO blackink_system",
	"""
	CREATE TABLE IF NOT EXISTS oauth_connect_nonces (
		nonce               VARCHAR(64)  PRIMARY KEY,
		client_id            VARCHAR(40)  NOT NULL REFERENCES clients(client_id),
		provider              VARCHAR(20)  NOT NULL,
		consumed_at            TIMESTAMPTZ,
		created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
		expires_at               TIMESTAMPTZ  NOT NULL
	)
	""",
	"CREATE INDEX IF NOT EXISTS ix_oauth_connect_nonces_client ON oauth_connect_nonces (client_id)",
	# nonce is a VARCHAR PK (the signed token's jti), not a BIGSERIAL — no
	# sequence grant needed here, unlike the other tables in this file.
	"GRANT SELECT, INSERT, UPDATE ON oauth_connect_nonces TO blackink_app",
	# A webhook notification arrives knowing only (provider, subscription_id)
	# — not yet a client_id — so an RLS-scoped session can't look up the
	# owning connection (session_scope's silent-zero-rows applies with no
	# client_id set). Same SECURITY DEFINER escape hatch already used
	# elsewhere in this repo for the non-poach check
	# (is_claimed_by_other_client(), see CLAUDE.md), not the BYPASSRLS
	# system role — that role is never imported from src/api/, and this
	# function returns only the minimal identity needed to then open a
	# properly client_id-scoped session for everything else.
	"""
	CREATE OR REPLACE FUNCTION resolve_calendar_connection(p_provider VARCHAR, p_subscription_id VARCHAR)
	RETURNS TABLE(connection_id BIGINT, client_id VARCHAR, verification_secret VARCHAR)
	LANGUAGE sql SECURITY DEFINER
	AS $$
		SELECT connection_id, client_id, verification_secret
		FROM calendar_connections
		WHERE provider = p_provider AND subscription_id = p_subscription_id AND status = 'ACTIVE'
	$$
	""",
	"GRANT EXECUTE ON FUNCTION resolve_calendar_connection(VARCHAR, VARCHAR) TO blackink_app",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_calendar_connections: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
