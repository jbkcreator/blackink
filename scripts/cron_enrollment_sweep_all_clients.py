"""Cron entry point: run the cold-sequence enrollment sweep for every active client.

`python -m src.tasks.enrollment_sweep` requires --client-id (an unscoped
sweep would silently see zero rows under RLS rather than fail loudly — see
CLAUDE.md). Cron needs one thing to run per tick, not one crontab line per
client, so this loops over `clients WHERE is_active` the same way
scripts/cron_work_orders_sweep_all_clients.py does and calls the same
run_sweep() the CLI uses.

Usage (see scripts/crontab.txt):
    PYTHONPATH=. python -m scripts.cron_enrollment_sweep_all_clients
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.tasks.enrollment_sweep import run_sweep

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    with get_system_db_context() as session:
        client_ids = [
            row.client_id
            for row in session.execute(text("SELECT client_id FROM clients WHERE is_active")).all()
        ]

    exit_code = 0
    total = 0
    for client_id in client_ids:
        try:
            total += run_sweep(client_id)
        except Exception:
            logger.exception("enrollment_sweep failed for client_id=%s", client_id)
            exit_code = 1
    logger.info("enrollment_sweep: enrolled %d contact(s) across %d client(s)", total, len(client_ids))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
