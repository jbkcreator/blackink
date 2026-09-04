import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

with get_owner_db_context() as db:
	row = db.execute(
		text("SELECT * FROM events WHERE event_type = 'meeting_booked' ORDER BY created_at DESC LIMIT 1")
	).first()
	print(dict(row._mapping) if row else "nothing yet")
