import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

connection_id = int(sys.argv[1]) if len(sys.argv) > 1 else 127

with get_owner_db_context() as db:
	row = db.execute(
		text("SELECT * FROM calendar_connections WHERE connection_id = :id"), {"id": connection_id}
	).one()
	print("Raw row:")
	for k, v in dict(row._mapping).items():
		print(f"  {k} = {v!r}")

	print()
	print("Calling resolve_calendar_connection('GOOGLE', subscription_id) directly:")
	resolved = db.execute(
		text("SELECT * FROM resolve_calendar_connection('GOOGLE', :sub)"), {"sub": row.subscription_id}
	).first()
	print(f"  result: {dict(resolved._mapping) if resolved else None}")
