"""Add data_coverage_pct column to owner_visibility_scores.

Subtask 2.1.2 — County Rank Calculator & Peer Benchmarking.

data_coverage_pct is `round((present_signals / 10) * 100)` where
present_signals = 10 - len(data_gaps). It is:
  - Written by score_one_company() at scoring time (upsert).
  - Used as the tie-breaking key in calculate_county_ranks() when two firms
    have identical score_total: higher coverage wins (spec 2.1.2, tie rule).
  - Included in the owner_visibility_score_calculated events payload.

SMALLINT is sufficient (0–100). NULL means the row was created before this
migration ran and has not been re-scored yet.

Idempotent: ADD COLUMN IF NOT EXISTS.

    PYTHONPATH=. python migrations/apply_ovs_data_coverage.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE owner_visibility_scores ADD COLUMN IF NOT EXISTS data_coverage_pct SMALLINT",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = {
            r.column_name
            for r in db.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'owner_visibility_scores'"
            )).fetchall()
        }
    assert "data_coverage_pct" in cols, "data_coverage_pct column missing after migration"
    print("apply_ovs_data_coverage: OK — data_coverage_pct SMALLINT added to owner_visibility_scores")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
