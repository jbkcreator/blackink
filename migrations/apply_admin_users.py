"""
Provision the admin_users table for internal dashboard authentication.

Not tenant-bearing. Idempotent.

    PYTHONPATH=. python migrations/apply_admin_users.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id            SERIAL          PRIMARY KEY,
        username      VARCHAR(100)    NOT NULL UNIQUE,
        password_hash TEXT            NOT NULL,
        created_at    TIMESTAMPTZ     NOT NULL DEFAULT NOW()
    )
    """,
    "GRANT SELECT ON admin_users TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON admin_users TO blackink_system",
    "GRANT USAGE ON SEQUENCE admin_users_id_seq TO blackink_system",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_admin_users: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
