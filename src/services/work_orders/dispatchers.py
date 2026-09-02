"""Execution dispatchers for APPROVED work orders — Dev 3 plan §3, item 1
("dispatchers"). Week 0 registers only `noop`: there is no real email/SMS
dispatch channel yet (that lands with the Week 1 Campaign Agent). Its job
is narrower but real — proving the state machine QUEUED -> APPROVED ->
EXECUTING -> DONE actually closes end to end, which is what AC #1's demo
needs to show.

DISPATCHERS is a plain dict, not a class registry, matching
FA/src/services/relay/channels.py's DISPATCHERS shape (referenced in the
Dev 3 plan's engine.py fork notes) — one lookup table, new channels added
by adding an entry, not subclassing anything.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict

from src.services.work_orders import WorkOrder

logger = logging.getLogger(__name__)


def noop_dispatch(order: WorkOrder) -> dict:
	"""Does nothing except prove the pipeline reaches this point. Returns
	the execution_receipt payload the caller persists via record_execution."""
	logger.info("[dispatchers] noop dispatch — action_id=%s action_class=%s", order.action_id, order.action_class)
	return {"dispatcher": "noop", "note": "Week 0 — no real channel registered yet"}


DISPATCHERS: Dict[str, Callable[[WorkOrder], dict]] = {
	"noop": noop_dispatch,
}
