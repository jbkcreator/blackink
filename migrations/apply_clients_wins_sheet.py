"""Subtask 3.2.5 Stage 6 (S-21) — per-client Client Wins Dashboard.

Adds clients.wins_sheet_id: the Google Sheet ID that the per-client wins
export (src/tasks/client_wins_sweep.py) writes this tenant's wins into, for
Looker Studio's Sheets connector to render. NULL until an operator provisions
a Sheet for the client and shares it with the export service account — the
sweep skips any client whose wins_sheet_id is NULL, so this is additive and
inert until set (same posture as the sandbox export's GOOGLE_SHEETS_* gate).

Not a new table and not tenant-bearing on its own — a nullable column on the
existing clients table (the tenant root itself), so no config/tenant_policies
entry and no RLS change. Existing clients grants already cover the column.

Rollback: additive only — `ALTER TABLE clients DROP COLUMN IF EXISTS
wins_sheet_id;` if it must be removed. Safe to leave in place.

Idempotent: ADD COLUMN IF NOT EXISTS. Run any time after apply_clients.py.

    PYTHONPATH=. python migrations/apply_clients_wins_sheet.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    "ALTER TABLE clients ADD COLUMN IF NOT EXISTS wins_sheet_id VARCHAR(120)",
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
    print("apply_clients_wins_sheet: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
