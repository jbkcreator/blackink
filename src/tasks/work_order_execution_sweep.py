"""All-tenant work-order execution sweep — the deployed runtime's dispatcher.

`sequence_sweep` only POSTS approval cards; clicking Approve moves a work
order to APPROVED but sends nothing. The actual dispatch is
`src.services.work_orders.__main__.cmd_sweep`, which is client-scoped and was
only ever invoked as a per-client CLI. Under the single-process deploy model
nothing ran it in production, so every APPROVED cadence touch (and every other
APPROVED order) sat un-dispatched forever.

This sweep closes that gap: it enumerates the distinct clients that currently
have APPROVED orders and runs the existing, proven `cmd_sweep` for each. Runs
cross-tenant via the system session only to discover which clients have work;
all execution, claiming, and RLS scoping stay inside cmd_sweep's per-client
sessions.

    python -m src.tasks.work_order_execution_sweep
"""

import logging

from src.services import work_orders as wo
from src.services.work_orders.__main__ import cmd_sweep

logger = logging.getLogger(__name__)


def run_sweep(limit: int = 200) -> int:
    """Dispatch APPROVED orders for every client that has any. Returns the
    number of clients swept."""
    batch = wo.approved_batch(client_id=None, limit=limit)
    client_ids = sorted({o.client_id for o in batch})
    if not client_ids:
        logger.debug("work_order_execution_sweep: no approved orders across any tenant")
        return 0

    for client_id in client_ids:
        try:
            cmd_sweep(client_id)
        except Exception:
            logger.exception("work_order_execution_sweep: cmd_sweep failed for client_id=%s", client_id)

    logger.info("work_order_execution_sweep: swept %d client(s)", len(client_ids))
    return len(client_ids)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    swept = run_sweep()
    print(f"work_order_execution_sweep: {swept} client(s) swept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
