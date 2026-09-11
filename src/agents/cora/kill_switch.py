"""Cora — combined halt check.

Two independent reasons to stop generating drafts:

  1. Relay halt (GLOBAL or CLIENT scope) — an admin issued an emergency stop
     via Slack. This is authoritative and cannot be overridden by the throttle.
     Cleared only by resume_halt() with a valid HMAC token.

  2. Throttle auto-pause — the unreviewed Slack approval queue has hit
     DRAFT_QUEUE_CAPACITY, OR its oldest unreviewed draft has been waiting
     longer than DRAFT_MAX_AGE_HOURS (W0 §3.0.2 C's second trigger). Clears
     automatically as human reviews drain the backlog (count case: below
     RESUME_THRESHOLD; age case: once the stale draft is itself reviewed).
     No admin action required.

cora_should_stop() is the single gate the worker loop calls. It checks both
in priority order so a Relay halt is never shadowed by a throttle state and
vice-versa.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.agents.relay.halt_service import is_halted
from src.agents.cora.throttle import is_auto_paused, pause_reason

logger = logging.getLogger(__name__)


def cora_should_stop(client_id: Optional[str] = None) -> bool:
    """True if the worker loop must idle without claiming new draft work.

    client_id narrows the Relay halt check to include CLIENT-scope halts for
    that client in addition to GLOBAL. Pass it whenever a specific client's
    event is about to be processed.
    """
    if is_halted(client_id=client_id):
        logger.warning(
            "cora: Relay halt active (client_id=%s) — idling without claiming new work",
            client_id,
        )
        return True

    if is_auto_paused():
        logger.warning(
            "cora: approval backlog auto-paused (trigger=%s) — not generating new drafts",
            pause_reason() or "unknown",
        )
        return True

    return False
