"""Clears a REACTIVATION pause once its target date has arrived (S-10,
W2 §3.2.1 — LATER-intent reactivation resume).

Mirrors no_show_recovery_dispatch.py's claim-time discipline: takes an
explicit `as_of` parameter (never NOW() internally) so the date boundary is
fast-forwardable in tests without a real wait. Runs BYPASSRLS (all clients)
since contacts carries no client_id of its own (join-scoped through
companies) — same posture as promotion_sweep.py.

Only ever clears a pause whose reason is EXACTLY 'REACTIVATION' — never
touches a contact paused for a different reason (e.g. an active
NO_SHOW_RECOVERY pause), matching src/services/reactivation.py's own
pause_contact_until() guard against clobbering that pause in the first
place.

    python -m src.tasks.reactivation_resume_sweep
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)


def run_sweep(*, as_of: Optional[datetime] = None) -> int:
    as_of = as_of or datetime.now(timezone.utc)
    with get_system_db_context() as db:
        rows = db.execute(
            text(
                "UPDATE contacts "
                "SET outbound_paused_at = NULL, "
                "    outbound_pause_reason = NULL, "
                "    outbound_pause_until = NULL, "
                "    updated_at = NOW() "
                "WHERE outbound_pause_reason = 'REACTIVATION' "
                "  AND outbound_pause_until IS NOT NULL "
                "  AND outbound_pause_until <= :as_of "
                "RETURNING contact_id"
            ),
            {"as_of": as_of},
        ).fetchall()
        db.commit()

    count = len(rows)
    if count:
        logger.info("reactivation_resume_sweep: resumed %d contact(s)", count)
    return count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_sweep()
