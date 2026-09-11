"""Per-client Client Wins Dashboard export sweep (Subtask 3.2.5 Stage 6 / S-21).

Refreshes every active client's wins Google Sheet on a schedule. "Real-time" in
the blueprint (§3.1.7) is, in practice, a short-interval refresh — the same
model as every other periodic job in this repo. Reads run under the system role
(BYPASSRLS), so this is an internal batch job, never imported from src/api/.

    PYTHONPATH=. python -m src.tasks.client_wins_sweep

Skips any client whose wins_sheet_id is NULL (not yet provisioned) and any
client where the export is unconfigured/unreachable — the export itself never
raises, so one client's Sheets outage cannot abort the others.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.client_wins import export_client_wins

logger = logging.getLogger(__name__)

_ACTIVE_CLIENTS_WITH_SHEET_SQL = """
    SELECT client_id, wins_sheet_id
    FROM clients
    WHERE is_active = TRUE AND wins_sheet_id IS NOT NULL
    ORDER BY client_id
"""


def run_sweep() -> int:
    """Export wins for every active client that has a wins_sheet_id. Returns the
    number of clients whose sheet was successfully written (rows > 0)."""
    with get_system_db_context() as session:
        clients = session.execute(text(_ACTIVE_CLIENTS_WITH_SHEET_SQL)).fetchall()

    exported = 0
    for client_id, sheet_id in clients:
        rows = export_client_wins(client_id, sheet_id)
        if rows > 0:
            exported += 1
        else:
            # Not fatal: NULL sheet is already filtered out above, so a 0 here
            # means the export was unconfigured or the Sheets call failed — both
            # already logged inside export_client_wins.
            logger.info("client_wins_sweep: no rows written for client %s", client_id)

    logger.info(
        "client_wins_sweep: done — %d/%d client sheets refreshed", exported, len(clients)
    )
    return exported


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_sweep()
