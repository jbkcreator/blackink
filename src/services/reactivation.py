"""LATER-intent reactivation (S-10, W2 §3.2.1).

Extracts a target reactivation date from a LATER-classified reply body and
pauses the contact's outbound sequence until that date, reusing the existing
contacts.outbound_paused_at / outbound_pause_reason mechanism (Subtask
3.2.3's No-Show Handler; already enforced at send time by
compliance_gate._check_not_paused — no new gate check needed) plus a new
outbound_pause_until date column that src/tasks/reactivation_resume_sweep.py
clears once due.

Date extraction is a best-effort parse (dateutil, fuzzy=True) bounded to a
sane future window. A reply with no clean date, or one outside that window,
is NOT silently guessed at — extract_target_date returns None and the
caller (src/agents/respond/worker.py) leaves requires_human_review=TRUE so a
human decides the actual reactivation date rather than the system inventing
one.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dateutil import parser as date_parser
from sqlalchemy import text

logger = logging.getLogger(__name__)

# A LATER reply plausibly points anywhere from tomorrow to next year — beyond
# that, treat it as unparseable-in-practice rather than trust a wild guess.
_MAX_FUTURE_DAYS = 365


def extract_target_date(reply_text: str, *, as_of: Optional[datetime] = None) -> Optional[datetime]:
    """Best-effort extraction of a future reactivation date from free text.
    Returns None if no date-shaped text is found, parsing fails, or the
    parsed date falls outside (as_of, as_of + _MAX_FUTURE_DAYS] — never a
    past date, never an implausibly-far-out one."""
    as_of = as_of or datetime.now(timezone.utc)
    if not reply_text or not reply_text.strip():
        return None
    try:
        parsed = date_parser.parse(reply_text, fuzzy=True, default=as_of)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed <= as_of or parsed > as_of + timedelta(days=_MAX_FUTURE_DAYS):
        return None
    return parsed


def pause_contact_until(session: Any, contact_id: int, target_date: datetime) -> bool:
    """Pauses outbound for contact_id until target_date. Idempotent — a
    later LATER reply simply moves the target date. Deliberately does NOT
    overwrite an existing pause for a DIFFERENT reason (e.g. an active
    NO_SHOW_RECOVERY pause) — reactivation defers to that pause rather than
    silently clobbering it; the WHERE clause is the guard. Returns True if
    the pause was applied (row existed and reason allowed it)."""
    result = session.execute(
        text(
            "UPDATE contacts "
            "SET outbound_paused_at = NOW(), "
            "    outbound_pause_reason = 'REACTIVATION', "
            "    outbound_pause_until = :target_date, "
            "    updated_at = NOW() "
            "WHERE contact_id = :contact_id "
            "  AND (outbound_pause_reason IS NULL OR outbound_pause_reason = 'REACTIVATION') "
            "RETURNING contact_id"
        ),
        {"contact_id": contact_id, "target_date": target_date},
    ).first()
    applied = result is not None
    if applied:
        logger.info("reactivation: contact_id=%s paused until %s", contact_id, target_date.isoformat())
    else:
        logger.info(
            "reactivation: contact_id=%s NOT paused — an existing non-REACTIVATION pause takes precedence",
            contact_id,
        )
    return applied
