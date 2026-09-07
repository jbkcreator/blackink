"""
Create or update an admin user for the internal dashboard.

Usage:
    PYTHONPATH=. python scripts/create_admin.py <username> <password>

Example:
    PYTHONPATH=. python scripts/create_admin.py admin s3cur3p@ss
"""
import sys

sys.path.insert(0, ".")

import bcrypt
from sqlalchemy import text

from src.core.database import get_system_db_context


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python scripts/create_admin.py <username> <password>")
        return 1

    username, password = sys.argv[1], sys.argv[2]
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    with get_system_db_context() as db:
        db.execute(
            text("""
                INSERT INTO admin_users (username, password_hash)
                VALUES (:username, :hash)
                ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash
            """),
            {"username": username, "hash": hashed},
        )
        db.commit()

    print(f"Admin user '{username}' saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
