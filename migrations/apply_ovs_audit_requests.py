"""
Provision the ovs_audit_requests table — stores inbound self-serve audit
requests submitted via audit.getblackink.com.

Not tenant-bearing (these are pre-client inbound leads; no client_id at
submission time). Do NOT add to TENANT_POLICIES or apply_rls_policies.

Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_ovs_audit_requests.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS ovs_audit_requests (
        id              BIGSERIAL       PRIMARY KEY,
        company_name    VARCHAR(200)    NOT NULL,
        domain          VARCHAR(200)    NOT NULL,
        contact_name    VARCHAR(200),
        email           VARCHAR(200)    NOT NULL,
        state           CHAR(2),
        status          VARCHAR(30)     NOT NULL DEFAULT 'pending',
        score_data      JSONB,
        pdf_url         TEXT,
        created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
        scored_at       TIMESTAMPTZ,
        client_id       VARCHAR(40)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_ovs_req_email ON ovs_audit_requests (email)",
    "CREATE INDEX IF NOT EXISTS ix_ovs_req_domain ON ovs_audit_requests (domain)",
    "CREATE INDEX IF NOT EXISTS ix_ovs_req_created ON ovs_audit_requests (created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE ON ovs_audit_requests TO blackink_app",
    "GRANT USAGE ON SEQUENCE ovs_audit_requests_id_seq TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON ovs_audit_requests TO blackink_system",
    "GRANT USAGE ON SEQUENCE ovs_audit_requests_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_ovs_audit_requests: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
