"""ALTER contacts to add prospect_objections — feeds the (future) Owner
Score engine per blueprint §3.1.7 / split-doc Subtask 4.2.2. Idempotent:
ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_contacts_prospect_objections.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
	"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS prospect_objections TEXT[] NOT NULL DEFAULT '{}'",
]


def main() -> int:
	with get_owner_db_context() as db:
		for stmt in DDL:
			db.execute(text(stmt))
		db.commit()
	print("apply_contacts_prospect_objections: done")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
