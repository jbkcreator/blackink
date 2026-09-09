"""Cron entry point: run the work-orders sweep for every active client.

`python -m src.services.work_orders --sweep` requires --client-id (an
unscoped sweep would silently see zero rows under RLS rather than fail
loudly — see CLAUDE.md). Cron needs one thing to run per tick, not one
crontab line per client, so this loops over `clients WHERE is_active` the
same way src/tasks/settlement_sweep.py already does and calls the same
cmd_sweep() the CLI uses.

Usage (see scripts/crontab.txt):
    PYTHONPATH=. python -m scripts.cron_work_orders_sweep_all_clients
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.work_orders.__main__ import cmd_sweep

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> int:
    with get_system_db_context() as session:
        client_ids = [row.client_id for row in session.execute(
            text("SELECT client_id FROM clients WHERE is_active")
        ).all()]

    exit_code = 0
    for client_id in client_ids:
        try:
            cmd_sweep(client_id)
        except Exception:
            logger.exception("work_orders sweep failed for client_id=%s", client_id)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
