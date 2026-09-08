"""
Add Speed-to-Lead columns to the clients table (Task 4.2.1).

New columns:
  inbound_webhook_secret_hash  — SHA-256 hex of the per-client shared
      secret used to authenticate Path A webhook calls. Provisioned by
      the onboarding runbook; NULL until set.

  subdomain_slug               — slug that appears in the Mailgun
      recipient address (e.g. "acme" → leads@acme.getblackink.com).
      Must be unique across active clients; NULL until provisioned.

  stl_reply_subject            — subject line for the 30-min SLA
      auto-response email. NULL falls back to a hardcoded default in
      the sweep ("We received your inquiry").

  stl_reply_html_template      — HTML body template for the SLA
      auto-response. Supports {{name}} and {{booking_link}} merge
      fields. NULL falls back to the sweep's inline default template.

Idempotent: ADD COLUMN IF NOT EXISTS.
Run AFTER apply_clients.py and BEFORE apply_rls_policies.py.

    PYTHONPATH=. python migrations/apply_clients_stl_fields.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS inbound_webhook_secret_hash VARCHAR(64)",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS subdomain_slug VARCHAR(60)",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS stl_reply_subject TEXT",
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS stl_reply_html_template TEXT",
    # Unique index — two clients can't share the same subdomain slug
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_clients_subdomain_slug ON clients (subdomain_slug) WHERE subdomain_slug IS NOT NULL",
    # ── Pre-tenant client resolution (SECURITY DEFINER) ──────────────────────
    # The clients table is RLS-scoped (direct client_id). An inbound webhook
    # has no tenant context yet — it must resolve client_id FROM the request
    # before session_scope(client_id) can open. A bare app session sees zero
    # clients rows under RLS, so these narrow SECURITY DEFINER functions do the
    # lookup (same pattern as resolve_calendar_connection in
    # apply_calendar_connections.py). They expose ONLY client_id, only for an
    # exact secret-hash / slug match on an active client — never a table scan.
    "DROP FUNCTION IF EXISTS resolve_client_by_webhook_secret(VARCHAR)",
    """
    CREATE OR REPLACE FUNCTION resolve_client_by_webhook_secret(p_secret_hash VARCHAR)
    RETURNS TABLE(client_id VARCHAR)
    SECURITY DEFINER
    SET search_path = pg_catalog
    LANGUAGE sql
    AS $$
        SELECT client_id FROM public.clients
        WHERE inbound_webhook_secret_hash = p_secret_hash AND is_active = TRUE
        LIMIT 1
    $$
    """,
    "REVOKE EXECUTE ON FUNCTION resolve_client_by_webhook_secret(VARCHAR) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION resolve_client_by_webhook_secret(VARCHAR) TO blackink_app",
    "ALTER FUNCTION resolve_client_by_webhook_secret(VARCHAR) OWNER TO CURRENT_USER",
    "DROP FUNCTION IF EXISTS resolve_client_by_subdomain(VARCHAR)",
    """
    CREATE OR REPLACE FUNCTION resolve_client_by_subdomain(p_slug VARCHAR)
    RETURNS TABLE(client_id VARCHAR)
    SECURITY DEFINER
    SET search_path = pg_catalog
    LANGUAGE sql
    AS $$
        SELECT client_id FROM public.clients
        WHERE subdomain_slug = p_slug AND is_active = TRUE
        LIMIT 1
    $$
    """,
    "REVOKE EXECUTE ON FUNCTION resolve_client_by_subdomain(VARCHAR) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION resolve_client_by_subdomain(VARCHAR) TO blackink_app",
    "ALTER FUNCTION resolve_client_by_subdomain(VARCHAR) OWNER TO CURRENT_USER",
]


def main() -> None:
    with get_owner_db_context() as session:
        for stmt in DDL:
            session.execute(text(stmt))
        session.commit()
    print("apply_clients_stl_fields: done")


if __name__ == "__main__":
    main()
