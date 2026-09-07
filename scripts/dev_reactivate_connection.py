import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

connection_id = int(sys.argv[1]) if len(sys.argv) > 1 else 127

with get_owner_db_context() as db:
	db.execute(
		text("UPDATE calendar_connections SET status = 'ACTIVE', updated_at = NOW() WHERE connection_id = :id"),
		{"id": connection_id},
	)
	db.commit()

print(f"connection {connection_id} reactivated")
