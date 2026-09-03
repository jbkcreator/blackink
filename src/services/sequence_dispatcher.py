"""At-most-once dispatch: INSERT claim → send → UPDATE SENT/FAILED."""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def claim_touch(
    session: Session,
    client_id: str,
    run_id: str,
    touch_step: int,
    mailbox_id: int,
) -> Optional[str]:
    """INSERT with ON CONFLICT DO NOTHING; returns dispatch_id or None if already claimed."""
    row = session.execute(
        text(
            "INSERT INTO sequence_touch_dispatches "
            "(client_id, run_id, touch_step, mailbox_id, status) "
            "VALUES (:client_id, :run_id, :touch_step, :mailbox_id, 'SENDING') "
            "ON CONFLICT (run_id, touch_step) DO NOTHING "
            "RETURNING dispatch_id"
        ),
        {
            "client_id": client_id,
            "run_id": run_id,
            "touch_step": touch_step,
            "mailbox_id": mailbox_id,
        },
    ).fetchone()
    if row is None:
        logger.warning(
            "sequence_dispatcher: touch already claimed run_id=%s touch_step=%d",
            run_id, touch_step,
        )
        return None
    return str(row.dispatch_id)


def mark_sent(
    session: Session,
    client_id: str,
    dispatch_id: str,
    message_id: str,
) -> bool:
    """UPDATE status=SENT. Returns False if row gone (reclaimed mid-flight)."""
    result = session.execute(
        text(
            "UPDATE sequence_touch_dispatches "
            "SET status = 'SENT', message_id = :message_id, sent_at = :now, updated_at = :now "
            "WHERE dispatch_id = :dispatch_id AND client_id = :client_id AND status = 'SENDING'"
        ),
        {
            "message_id": message_id,
            "now": datetime.now(timezone.utc),
            "dispatch_id": dispatch_id,
            "client_id": client_id,
        },
    )
    if result.rowcount == 0:
        logger.error(
            "sequence_dispatcher: mark_sent rowcount=0 — dispatch_id=%s may have been reclaimed",
            dispatch_id,
        )
        return False
    return True


def mark_failed(
    session: Session,
    client_id: str,
    dispatch_id: str,
    reason: str,
) -> None:
    """UPDATE status=FAILED."""
    session.execute(
        text(
            "UPDATE sequence_touch_dispatches "
            "SET status = 'FAILED', updated_at = :now "
            "WHERE dispatch_id = :dispatch_id AND client_id = :client_id"
        ),
        {
            "now": datetime.now(timezone.utc),
            "dispatch_id": dispatch_id,
            "client_id": client_id,
        },
    )
    logger.warning("sequence_dispatcher: dispatch_id=%s FAILED reason=%s", dispatch_id, reason)


def find_stuck_dispatches(session: Session, older_than_minutes: int = 30) -> list:
    """Rows stuck in SENDING past the reclaim window — a worker died between
    claim and mark_sent/mark_failed. These are NOT auto-retried (ticket 22:
    a retry risks a double-send); they are surfaced to #blackink-qa for a human
    to reconcile. Returns a list of (dispatch_id, client_id, run_id, touch_step,
    created_at) tuples. Cross-client, so call under a system (BYPASSRLS) session."""
    rows = session.execute(
        text(
            "SELECT dispatch_id, client_id, run_id, touch_step, created_at "
            "FROM sequence_touch_dispatches "
            "WHERE status = 'SENDING' "
            "AND created_at < NOW() - make_interval(mins => :mins) "
            "ORDER BY created_at ASC"
        ),
        {"mins": older_than_minutes},
    ).fetchall()
    return list(rows)


def daily_sends_for_mailbox(session: Session, mailbox_id: int, client_id: str) -> int:
    """Count SENDING/SENT dispatches for this mailbox in the last 24 hours."""
    count = session.execute(
        text(
            "SELECT COUNT(*) FROM sequence_touch_dispatches "
            "WHERE mailbox_id = :mailbox_id "
            "AND client_id = :client_id "
            "AND status IN ('SENDING', 'SENT') "
            "AND created_at >= NOW() - INTERVAL '24 hours'"
        ),
        {"mailbox_id": mailbox_id, "client_id": client_id},
    ).scalar()
    return count or 0
