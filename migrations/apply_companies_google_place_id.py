"""
Add google_place_id column to companies (Dev 2, Owner Visibility Score engine).

google_place_id is populated once per firm by the owner_visibility_sweep when
the Google Places API key is present (stub provider leaves it NULL). Storing it
avoids a Places text-search call on every monthly sweep — the Places lookup is
expensive and rate-limited; resolving it once and caching the place_id is the
pattern Google themselves recommend for this use case.

Idempotent: ADD COLUMN IF NOT EXISTS (Postgres 9.6+).

    PYTHONPATH=. python migrations/apply_companies_google_place_id.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS google_place_id VARCHAR(255)",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        has_col = db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'companies' AND column_name = 'google_place_id'"
            )
        ).scalar()
    status = "present" if has_col else "MISSING — check migration output above"
    print(f"apply_companies_google_place_id: done — google_place_id column {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
