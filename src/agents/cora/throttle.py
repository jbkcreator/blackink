"""Cora draft-generation throttle — approval backlog bounds.

W0 §3.0.2 C names TWO independent capacity triggers: 50 unreviewed drafts,
OR any single unreviewed draft older than 24 hours. Both feed the same
auto-pause outcome; only the first was originally built (S-2 in the Week
0-2 implementation audit added the second).

Three Redis keys govern the throttle:
  cora:approval:pending  — integer counter: how many drafts are currently
                           waiting for a Slack approval/rejection decision.
                           Incremented by notify_draft_queued(), decremented
                           by notify_approval_resolved().
  cora:approval:queued_at — ZSET, member=draft_id, score=queued-at timestamp:
                           one entry ZADDed per notify_draft_queued(draft_id)
                           call, ZREMed by notify_approval_resolved(draft_id)
                           when that SAME draft_id resolves. Never itself the
                           source of the pause decision — is_auto_paused()
                           reads only its lowest-score member, live, on every
                           call.

                           Code-review fix (PR #50, finding 7): this was
                           previously a plain FIFO LIST with no identity —
                           notify_approval_resolved() always popped whatever
                           was at the head, regardless of which draft
                           actually resolved. notify_approval_resolved() is
                           called from more than one Slack decision handler,
                           including a GENERIC work-order handler used for
                           approval types that have nothing to do with Cora
                           — an unrelated approval could therefore remove the
                           timestamp for a still-unreviewed Cora draft,
                           silently preventing the 24h pause from ever
                           triggering for it. A ZSET keyed by the resolving
                           order's own action_id makes this self-correcting
                           without the caller needing to know whether a given
                           order is actually a Cora draft: ZREM on an
                           action_id that was never ZADDed by Cora's own
                           worker (i.e. any non-Cora approval) is a normal,
                           documented Redis no-op — it removes nothing,
                           rather than corrupting an unrelated entry.
  cora:auto_paused       — presence-flag (value "1"): set when pending count
                           reaches DRAFT_QUEUE_CAPACITY, cleared when it falls
                           below RESUME_THRESHOLD. Governs ONLY the count
                           trigger — see is_auto_paused() for why the age
                           trigger deliberately does not use a sticky flag.

Hysteresis (RESUME_THRESHOLD < DRAFT_QUEUE_CAPACITY) prevents the worker from
cycling in and out of auto-pause every time a single review clears exactly at
the boundary, which would generate burst bursts at the edge of capacity. The
age trigger needs no hysteresis of its own: a live "is the oldest entry over
24h" check only ever flips true->false when that entry is actually popped
(reviewed), not through any continuous fluctuation a boundary could flap on.

Why the age check must be LIVE, not a second sticky flag set reactively
inside notify_draft_queued()/notify_approval_resolved(): is_auto_paused() is
read on every worker-loop iteration via cora_should_stop() (see
kill_switch.py), not only when a queue/dequeue event fires. A single stale
draft sitting in an otherwise-idle queue (no new drafts arriving, nobody
reviewing) would never re-trigger a reactive check — a live read-time
computation has no such gap and needs no new scheduled job.

NOT the same queue as src.services.work_orders.queued_depth(), which counts
agent_work_orders rows (sequence-touch / win-back / STL-cadence / meeting-
outcome approval cards — four unrelated subsystems). Cora's drafts never
create a row there; see docs/plans/2026-09-10-s2-cora-24h-age-throttle.md
§2 for the full trace of why that helper is not used here.

These keys are NOT tenant-scoped — the approval queue is a global platform
resource for week 0. If per-client throttles are needed later, scope the keys
with a client_id prefix and update notify_*/is_auto_paused() accordingly.

Dev 3 (Slack bot) calls notify_approval_resolved() when an operator clicks
Approve/Reject on a draft card. This module only tracks counts/timestamps —
it does not know or care about card content.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

DRAFT_QUEUE_CAPACITY: int = 50
RESUME_THRESHOLD: int = 40
DRAFT_MAX_AGE_HOURS: int = 24

_PENDING_KEY = "cora:approval:pending"
_QUEUED_AT_KEY = "cora:approval:queued_at"
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


def _oldest_queued_age_seconds() -> Optional[float]:
    """Age in seconds of the earliest still-outstanding queued draft (the
    lowest-score member of the ZSET), or None if the set is empty or Redis
    is unreachable/the stored value is corrupt. None means "no age-based
    pause condition detected" — same fail-open posture is_auto_paused()
    already documents for this automatic (non-compliance) guard."""
    try:
        oldest = get_redis_client().zrange(_QUEUED_AT_KEY, 0, 0, withscores=True)
    except Exception as exc:
        logger.error("cora.throttle: failed to read oldest-queued timestamp: %s", exc)
        return None
    if not oldest:
        return None
    _member, score = oldest[0]
    try:
        return time.time() - float(score)
    except (TypeError, ValueError) as exc:
        logger.error("cora.throttle: corrupt oldest-queued score %r: %s", score, exc)
        return None


def _count_flag_set() -> bool:
    """Reads ONLY the sticky count-based auto-pause flag — deliberately
    separate from is_auto_paused()'s combined (count OR age) result.
    _check_capacity()/_maybe_resume() must call THIS, not is_auto_paused():
    if they used the combined result, an age-only pause (count nowhere near
    capacity) would make _maybe_resume() think a count-triggered pause was
    active and log a bogus "auto-resumed", and _check_capacity() would skip
    setting the real count flag on a tick where age also happens to be
    breached — leaving the count trigger unrecorded (and thus not resumable
    correctly by count logic once the stale item is eventually popped and
    the age condition clears on its own).

    Fails open (False) on Redis error — same reasoning as is_halted()'s
    fail-open: prefer availability over accidentally locking all workers
    when Redis is degraded. The Relay halt layer provides the safety
    backstop for admin-ordered stops; the throttle is an automatic quality
    guard, not a compliance gate.
    """
    try:
        return bool(get_redis_client().exists(_AUTO_PAUSED_KEY))
    except Exception as exc:
        logger.error("cora.throttle: failed to check auto-pause flag: %s", exc)
        return False


def is_auto_paused() -> bool:
    """True if EITHER capacity trigger from W0 §3.0.2 C is active:

      - the sticky count-based auto-pause flag (set/cleared by
        _check_capacity()/_maybe_resume() with RESUME_THRESHOLD hysteresis), OR
      - the oldest still-queued draft has been waiting longer than
        DRAFT_MAX_AGE_HOURS — computed LIVE on every call (no sticky flag;
        see module docstring for why a live check is required here and why
        it needs no hysteresis of its own).

    A Redis error reading the count flag fails open (via _count_flag_set())
    but does NOT short-circuit the age check — the two failure domains are
    independent, so a failure reading one piece of state must not mask an
    independently-successful read that shows a real problem in the other.
    """
    if _count_flag_set():
        return True

    age = _oldest_queued_age_seconds()
    return age is not None and age > DRAFT_MAX_AGE_HOURS * 3600


def pause_reason() -> Optional[str]:
    """Diagnostic only — which trigger(s) are currently causing
    is_auto_paused() to return True: "count", "age", "count+age", or None.

    Code-review finding: kill_switch.py's log line used to say only
    "approval backlog at capacity" regardless of cause, so an operator
    debugging a paused worker had no way to tell — from logs alone —
    whether the 50-draft cap was hit or a single draft had simply gone
    unreviewed for over a day. cora_should_stop() calls this to make the
    log line specific.
    """
    reasons = []
    if _count_flag_set():
        reasons.append("count")
    age = _oldest_queued_age_seconds()
    if age is not None and age > DRAFT_MAX_AGE_HOURS * 3600:
        reasons.append("age")
    return "+".join(reasons) if reasons else None


# ── Write helpers ─────────────────────────────────────────────────────────────

def _check_capacity(count: int) -> None:
    """Set auto-pause if count has reached or exceeded DRAFT_QUEUE_CAPACITY."""
    if count >= DRAFT_QUEUE_CAPACITY and not _count_flag_set():
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
    if count < RESUME_THRESHOLD and _count_flag_set():
        try:
            get_redis_client().delete(_AUTO_PAUSED_KEY)
            logger.info(
                "cora.throttle: approval backlog=%d < resume_threshold=%d — auto-resumed",
                count, RESUME_THRESHOLD,
            )
        except Exception as exc:
            logger.error("cora.throttle: failed to clear auto-pause flag: %s", exc)


# ── Event hooks (called by the worker / Slack bot) ────────────────────────────

def notify_draft_queued(draft_id: str) -> int:
    """Call when a draft has been generated and sent to Slack for review.

    Increments the approval backlog counter and adds this draft's
    queued-at timestamp to the age-tracking ZSET, keyed by draft_id,
    ATOMICALLY (a Redis pipeline, not two independent calls) — code-review
    finding: two separate calls left a crash window where a process death
    between them permanently orphaned the counter and the ZSET against
    each other (the counter says N pending but only N-1 are ever tracked
    for staleness, forever — nothing later re-syncs them). A pipeline
    closes that window; either both writes land or neither does.

    `draft_id` must be a stable identifier for this specific draft (its
    work order's own action_id) — notify_approval_resolved() uses the SAME
    id to remove exactly this entry when it resolves, not whatever happens
    to be oldest (see module docstring, PR #50 finding 7).

    Then enforces the count-based capacity trigger (the age trigger needs
    no enforcement call here — it is computed live by is_auto_paused() on
    every read; see module docstring). Returns the new count, or -1 on
    Redis error.
    """
    try:
        r = get_redis_client()
        pipe = r.pipeline()
        pipe.incr(_PENDING_KEY)
        pipe.zadd(_QUEUED_AT_KEY, {draft_id: time.time()})
        count, _ = pipe.execute()
        _check_capacity(count)
        return count
    except Exception as exc:
        logger.error("cora.throttle: notify_draft_queued failed: %s", exc)
        return -1


def notify_approval_resolved(draft_id: Optional[str] = None) -> int:
    """Call when an operator approves or rejects a draft in Slack (Dev 3).

    Decrements the approval backlog counter and removes the matching entry
    from the age-tracking ZSET ATOMICALLY (a Redis pipeline) — same
    crash-window fix as notify_draft_queued().

    Code-review fix (PR #50, finding 7): this used to unconditionally pop
    the OLDEST entry off a FIFO list, with no identity check. This function
    is called from more than one Slack decision handler, including a
    GENERIC work-order handler used for approval types that have nothing
    to do with Cora (see docs/plans/2026-09-10-s2-cora-24h-age-throttle.md
    §2) — popping the head regardless of which draft actually resolved
    meant an unrelated approval could silently remove the timestamp for a
    still-unreviewed Cora draft, preventing its 24h pause from ever
    triggering. Passing `draft_id` (the resolving order's own action_id)
    fixes this: ZREM only removes that exact member, and is a safe no-op
    if it was never ZADDed by Cora's own worker in the first place (i.e.
    any non-Cora approval correctly touches nothing here). Callers that
    genuinely cannot supply an id (none exist in this codebase currently,
    but kept for backward compatibility) may omit it — the count still
    decrements, but no ZSET entry is touched, which is the safe direction
    (a stale entry lingering an extra cycle, never a wrongly-removed one).

    Lifts the count-based auto-pause if the backlog has dropped below
    RESUME_THRESHOLD (the age-based pause, if any, lifts on its own the
    moment the matching entry is removed and it was the stale one — see
    is_auto_paused()). Returns the new count (floor 0), or -1 on Redis
    error.
    """
    try:
        r = get_redis_client()
        pipe = r.pipeline()
        pipe.decr(_PENDING_KEY)
        if draft_id is not None:
            pipe.zrem(_QUEUED_AT_KEY, draft_id)
        results = pipe.execute()
        count = results[0]
        if count < 0:
            r.set(_PENDING_KEY, "0")
            count = 0
        _maybe_resume(count)
        return count
    except Exception as exc:
        logger.error("cora.throttle: notify_approval_resolved failed: %s", exc)
        return -1
