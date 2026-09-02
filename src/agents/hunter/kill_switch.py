"""Hunter — halt check.

Delegates to Relay's halt_service.is_halted() for GLOBAL-scope halts.
Hunter has no per-client scope (it sweeps all tenants via BYPASSRLS) so
only GLOBAL halts are checked, not CLIENT or CAMPAIGN.
"""
from __future__ import annotations

import logging

from src.agents.relay.halt_service import is_halted

logger = logging.getLogger(__name__)


def hunter_should_stop() -> bool:
    """True if the nightly sweep should not start (or should abort mid-run)."""
    if is_halted():
        logger.warning("hunter: Relay GLOBAL halt active — skipping sweep")
        return True
    return False
