"""
Band 2 consecutive-clean-send counter and promotion gate (S-9).

BAND_2_ONE_TAP work orders require a human to tap Approve before dispatch.
After PROMOTION_THRESHOLD consecutive clean sends (outcome == 'SENT') for a
(client_id, action_class, touch_step) combination, the system earns
BAND_3_AUTO and bypasses the approval card.

A single failure resets the streak to 0 — the client must rebuild. promoted_at
is stamped once and never cleared, so a class that earned auto-dispatch keeps
the timestamp as an audit trail even after a reset, but resolve_band() only
reads clean_streak (not promoted_at) to decide the current band.

Only tracked action classes participate. Untracked classes always return
BAND_2_ONE_TAP (fail-closed default).

Writes use get_system_db_context — band2_counters has no RLS.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)

PROMOTION_THRESHOLD = 50

# Action classes whose successful sends build toward BAND_3_AUTO.
# Dial, LinkedIn, and STL arm actions are explicitly excluded:
# — manual tasks (DIAL/LINKEDIN) have no "send" outcome to validate
# — STL cadence arm (STL_CADENCE_ARM) is an internal gate, not a customer touch
TRACKED_CLASSES: frozenset[str] = frozenset(
    {"DISPATCH_EMAIL_TOUCH", "DISPATCH_WINBACK_TOUCH"}
)


def resolve_band(client_id: str, action_class: str, touch_step: int = 0) -> str:
    """Return the appropriate autonomy band for a new work order.

    Returns BAND_3_AUTO when the streak has reached PROMOTION_THRESHOLD,
    BAND_2_ONE_TAP otherwise. Untracked action classes always return
    BAND_2_ONE_TAP. Never raises — falls back to BAND_2_ONE_TAP on any DB error
    so a counter-table problem never silently causes unintended auto-sends."""
    if action_class not in TRACKED_CLASSES:
        return "BAND_2_ONE_TAP"
    try:
        with get_system_db_context() as session:
            row = session.execute(
                text(
                    "SELECT clean_streak FROM band2_counters "
                    "WHERE client_id = :client_id "
                    "  AND action_class = :action_class "
                    "  AND touch_step  = :touch_step"
                ),
                {"client_id": client_id, "action_class": action_class, "touch_step": touch_step},
            ).first()
    except Exception:
        logger.error(
            "autonomy_band.resolve_band: DB error for client=%s action=%s step=%s — defaulting BAND_2",
            client_id, action_class, touch_step, exc_info=True,
        )
        return "BAND_2_ONE_TAP"
    if row and row.clean_streak >= PROMOTION_THRESHOLD:
        return "BAND_3_AUTO"
    return "BAND_2_ONE_TAP"


def record_clean_send(client_id: str, action_class: str, touch_step: int = 0) -> None:
    """Increment the clean-send streak by 1. Stamps promoted_at the first time
    the streak crosses PROMOTION_THRESHOLD. Idempotent INSERT … ON CONFLICT."""
    if action_class not in TRACKED_CLASSES:
        return
    try:
        with get_system_db_context() as session:
            session.execute(
                text(
                    """
                    INSERT INTO band2_counters
                        (client_id, action_class, touch_step, clean_streak, updated_at)
                    VALUES
                        (:client_id, :action_class, :touch_step, 1, NOW())
                    ON CONFLICT (client_id, action_class, touch_step) DO UPDATE
                    SET
                        clean_streak = band2_counters.clean_streak + 1,
                        promoted_at  = CASE
                            WHEN band2_counters.promoted_at IS NULL
                                 AND band2_counters.clean_streak + 1 >= :threshold
                            THEN NOW()
                            ELSE band2_counters.promoted_at
                        END,
                        updated_at   = NOW()
                    """
                ),
                {
                    "client_id":    client_id,
                    "action_class": action_class,
                    "touch_step":   touch_step,
                    "threshold":    PROMOTION_THRESHOLD,
                },
            )
            session.commit()
        logger.debug(
            "autonomy_band.record_clean_send: client=%s action=%s step=%s streak +1",
            client_id, action_class, touch_step,
        )
    except Exception:
        logger.error(
            "autonomy_band.record_clean_send: DB error for client=%s action=%s step=%s — streak not recorded",
            client_id, action_class, touch_step, exc_info=True,
        )


def record_failed_send(client_id: str, action_class: str, touch_step: int = 0) -> None:
    """Reset the clean-send streak to 0 on a dispatch failure.
    promoted_at is preserved as an audit trail of when auto-send was earned."""
    if action_class not in TRACKED_CLASSES:
        return
    try:
        with get_system_db_context() as session:
            session.execute(
                text(
                    """
                    INSERT INTO band2_counters
                        (client_id, action_class, touch_step, clean_streak, updated_at)
                    VALUES
                        (:client_id, :action_class, :touch_step, 0, NOW())
                    ON CONFLICT (client_id, action_class, touch_step) DO UPDATE
                    SET clean_streak = 0,
                        updated_at   = NOW()
                    """
                ),
                {"client_id": client_id, "action_class": action_class, "touch_step": touch_step},
            )
            session.commit()
        logger.debug(
            "autonomy_band.record_failed_send: client=%s action=%s step=%s streak reset to 0",
            client_id, action_class, touch_step,
        )
    except Exception:
        logger.error(
            "autonomy_band.record_failed_send: DB error for client=%s action=%s step=%s — reset not recorded",
            client_id, action_class, touch_step, exc_info=True,
        )
