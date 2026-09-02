"""Cora draft-generation throttle — approval backlog bounds.

Two Redis keys govern the throttle:
  cora:approval:pending  — integer counter: how many drafts are currently
                           waiting for a Slack approval/rejection decision.
                           Incremented by notify_draft_queued(), decremented
                           by notify_approval_resolved().
  cora:auto_paused       — presence-flag (value "1"): set when pending count
                           reaches DRAFT_QUEUE_CAPACITY, cleared when it falls
                           below RESUME_THRESHOLD.

Hysteresis (RESUME_THRESHOLD < DRAFT_QUEUE_CAPACITY) prevents the worker from
cycling in and out of auto-pause every time a single review clears exactly at
the boundary, which would generate burst bursts at the edge of capacity.

These keys are NOT tenant-scoped — the approval queue is a global platform
resource for week 0. If per-client throttles are needed later, scope the keys
with a client_id prefix and update notify_*/is_auto_paused() accordingly.

Dev 3 (Slack bot) calls notify_approval_resolved() when an operator clicks
Approve/Reject on a draft card. This module only tracks counts — it does not
know or care about card content.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

DRAFT_QUEUE_CAPACITY: int = 50
RESUME_THRESHOLD: int = 40

_PENDING_KEY = "cora:approval:pending"
_AUTO_PAUSED_KEY = "cora:auto_paused"


# ── Read helpers ──────────────────────────────────────────────────────────────

def approval_pending_count() -> int:
    """Current number of drafts awaiting Slack approval/rejection.

    Returns 0 on Redis error — callers must not interpret 0 as "definitely
    below capacity." If is_auto_paused() is True, treat the queue as full
    regardless of what this returns.
    """
    try:
        raw = get_redis_client().get(_PENDING_KEY)
        return int(raw) if raw is not None else 0
    except Exception as exc:
        logger.error("cora.throttle: failed to read pending count: %s", exc)
        return 0


def is_auto_paused() -> bool:
    """True if the throttle has set the auto-pause flag.

    Fails open (False) on Redis error — same reasoning as is_halted()'s
    fail-open: prefer availability over accidentally locking all workers when
    Redis is degraded. The Relay halt layer provides the safety backstop for
    admin-ordered stops; the throttle is an automatic quality guard, not a
    compliance gate.
    """
    try:
        return bool(get_redis_client().exists(_AUTO_PAUSED_KEY))
    except Exception as exc:
        logger.error("cora.throttle: failed to check auto-pause flag: %s", exc)
        return False


# ── Write helpers ─────────────────────────────────────────────────────────────

def _check_capacity(count: int) -> None:
    """Set auto-pause if count has reached or exceeded DRAFT_QUEUE_CAPACITY."""
    if count >= DRAFT_QUEUE_CAPACITY and not is_auto_paused():
        try:
            get_redis_client().set(_AUTO_PAUSED_KEY, "1")
            logger.warning(
                "cora.throttle: approval backlog=%d >= capacity=%d — auto-paused",
                count, DRAFT_QUEUE_CAPACITY,
            )
        except Exception as exc:
            logger.error("cora.throttle: failed to set auto-pause flag: %s", exc)


def _maybe_resume(count: int) -> None:
    """Clear auto-pause if count has dropped below RESUME_THRESHOLD."""
    if count < RESUME_THRESHOLD and is_auto_paused():
        try:
            get_redis_client().delete(_AUTO_PAUSED_KEY)
            logger.info(
                "cora.throttle: approval backlog=%d < resume_threshold=%d — auto-resumed",
                count, RESUME_THRESHOLD,
            )
        except Exception as exc:
            logger.error("cora.throttle: failed to clear auto-pause flag: %s", exc)


# ── Event hooks (called by the worker / Slack bot) ────────────────────────────

def notify_draft_queued() -> int:
    """Call when a draft has been generated and sent to Slack for review.

    Increments the approval backlog counter and enforces capacity.
    Returns the new count, or -1 on Redis error.
    """
    try:
        count = get_redis_client().incr(_PENDING_KEY)
        _check_capacity(count)
        return count
    except Exception as exc:
        logger.error("cora.throttle: notify_draft_queued failed: %s", exc)
        return -1


def notify_approval_resolved() -> int:
    """Call when an operator approves or rejects a draft in Slack (Dev 3).

    Decrements the approval backlog counter and lifts the auto-pause if the
    backlog has dropped below RESUME_THRESHOLD.
    Returns the new count (floor 0), or -1 on Redis error.
    """
    try:
        r = get_redis_client()
        count = r.decr(_PENDING_KEY)
        if count < 0:
            r.set(_PENDING_KEY, "0")
            count = 0
        _maybe_resume(count)
        return count
    except Exception as exc:
        logger.error("cora.throttle: notify_approval_resolved failed: %s", exc)
        return -1
