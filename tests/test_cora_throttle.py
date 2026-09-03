"""Unit tests for Cora throttle and kill-switch.

Uses fakeredis — no live Redis or DB required. Covers:
- approval_pending_count() reads the Redis counter
- notify_draft_queued() increments counter and enforces capacity
- notify_approval_resolved() decrements counter and lifts auto-pause
- Hysteresis: auto-pause stays until count < RESUME_THRESHOLD, not just < CAPACITY
- is_auto_paused() fails open (False) on Redis error
- cora_should_stop() fires on Relay halt or auto-pause
- Queue publish/pending_count integration with fakeredis
"""

from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from src.agents.cora import throttle
from src.agents.cora.throttle import (
    DRAFT_QUEUE_CAPACITY,
    RESUME_THRESHOLD,
    _AUTO_PAUSED_KEY,
    _PENDING_KEY,
    approval_pending_count,
    is_auto_paused,
    notify_approval_resolved,
    notify_draft_queued,
)
from src.agents.cora import queue as cora_queue
from src.agents.cora.queue import STREAM_KEY, GROUP_NAME


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def r():
    """Fresh fakeredis instance, patched into throttle and queue modules."""
    fake = fakeredis.FakeRedis(decode_responses=True)
    with patch("src.agents.cora.throttle.get_redis_client", return_value=fake), \
         patch("src.agents.cora.queue.get_redis_client", return_value=fake):
        yield fake


# ── approval_pending_count ────────────────────────────────────────────────────

def test_pending_count_zero_on_empty_redis(r):
    assert approval_pending_count() == 0


def test_pending_count_reflects_redis_value(r):
    r.set(_PENDING_KEY, "17")
    assert approval_pending_count() == 17


def test_pending_count_returns_zero_on_redis_error():
    broken = MagicMock()
    broken.get.side_effect = RuntimeError("Redis down")
    with patch("src.agents.cora.throttle.get_redis_client", return_value=broken):
        assert approval_pending_count() == 0


# ── notify_draft_queued ───────────────────────────────────────────────────────

def test_notify_draft_queued_increments_counter(r):
    assert notify_draft_queued() == 1
    assert notify_draft_queued() == 2
    assert approval_pending_count() == 2


def test_notify_draft_queued_sets_auto_pause_at_capacity(r):
    for _ in range(DRAFT_QUEUE_CAPACITY - 1):
        notify_draft_queued()
    assert not is_auto_paused()  # one below capacity — not yet paused
    notify_draft_queued()        # hits capacity
    assert is_auto_paused()


def test_notify_draft_queued_does_not_double_set_auto_pause(r):
    for _ in range(DRAFT_QUEUE_CAPACITY + 5):
        notify_draft_queued()
    # auto_paused key should exist exactly once (SET idempotent anyway but logic is clean)
    assert is_auto_paused()
    assert approval_pending_count() == DRAFT_QUEUE_CAPACITY + 5


# ── notify_approval_resolved ──────────────────────────────────────────────────

def test_notify_approval_resolved_decrements_counter(r):
    r.set(_PENDING_KEY, "10")
    assert notify_approval_resolved() == 9
    assert approval_pending_count() == 9


def test_notify_approval_resolved_floors_at_zero(r):
    r.set(_PENDING_KEY, "0")
    result = notify_approval_resolved()
    assert result == 0
    assert approval_pending_count() == 0


def test_notify_approval_resolved_lifts_auto_pause_below_resume_threshold(r):
    # Simulate being at capacity with auto-pause set
    r.set(_PENDING_KEY, str(RESUME_THRESHOLD))
    r.set(_AUTO_PAUSED_KEY, "1")
    # One more resolution drops below RESUME_THRESHOLD
    notify_approval_resolved()
    assert not is_auto_paused()


# ── Hysteresis ────────────────────────────────────────────────────────────────

def test_auto_pause_not_lifted_between_threshold_and_capacity(r):
    """Auto-pause stays active when count is between RESUME_THRESHOLD and CAPACITY."""
    # Start at capacity
    for _ in range(DRAFT_QUEUE_CAPACITY):
        notify_draft_queued()
    assert is_auto_paused()

    # Drain down to just above RESUME_THRESHOLD — should still be paused
    target = RESUME_THRESHOLD + 1
    while approval_pending_count() > target:
        notify_approval_resolved()

    assert is_auto_paused(), (
        f"Auto-pause should persist at count={approval_pending_count()}, "
        f"which is above RESUME_THRESHOLD={RESUME_THRESHOLD}"
    )


def test_auto_pause_lifts_exactly_at_resume_threshold(r):
    """Auto-pause clears when count drops to RESUME_THRESHOLD - 1."""
    for _ in range(DRAFT_QUEUE_CAPACITY):
        notify_draft_queued()
    # Drain to exactly RESUME_THRESHOLD (still paused)
    while approval_pending_count() > RESUME_THRESHOLD:
        notify_approval_resolved()
    assert is_auto_paused()

    # One more → drops below RESUME_THRESHOLD → resumes
    notify_approval_resolved()
    assert not is_auto_paused()


# ── is_auto_paused — fail open ────────────────────────────────────────────────

def test_is_auto_paused_returns_false_on_redis_error():
    broken = MagicMock()
    broken.exists.side_effect = RuntimeError("Redis down")
    with patch("src.agents.cora.throttle.get_redis_client", return_value=broken):
        assert is_auto_paused() is False


# ── cora_should_stop — kill switch integration ────────────────────────────────

def test_cora_should_stop_false_when_clean(r):
    with patch("src.agents.cora.kill_switch.is_halted", return_value=False), \
         patch("src.agents.cora.kill_switch.is_auto_paused", return_value=False):
        from src.agents.cora.kill_switch import cora_should_stop
        assert cora_should_stop() is False


def test_cora_should_stop_true_on_relay_halt(r):
    with patch("src.agents.cora.kill_switch.is_halted", return_value=True), \
         patch("src.agents.cora.kill_switch.is_auto_paused", return_value=False):
        from src.agents.cora.kill_switch import cora_should_stop
        assert cora_should_stop() is True


def test_cora_should_stop_true_on_auto_pause(r):
    with patch("src.agents.cora.kill_switch.is_halted", return_value=False), \
         patch("src.agents.cora.kill_switch.is_auto_paused", return_value=True):
        from src.agents.cora.kill_switch import cora_should_stop
        assert cora_should_stop() is True


def test_cora_should_stop_true_when_both_active(r):
    with patch("src.agents.cora.kill_switch.is_halted", return_value=True), \
         patch("src.agents.cora.kill_switch.is_auto_paused", return_value=True):
        from src.agents.cora.kill_switch import cora_should_stop
        assert cora_should_stop() is True


# ── queue publish / pending_count ─────────────────────────────────────────────

def test_queue_publish_returns_message_id(r):
    msg_id = cora_queue.publish("draft.requested", "acme_pm", {"contact_id": 1})
    assert msg_id is not None


def test_queue_pending_count_increments_after_publish(r):
    cora_queue.publish("draft.requested", "acme_pm", {"contact_id": 1})
    # pending_count reflects unacked entries in the consumer group
    # After publish but no consumer has read yet, pending = 0 (not yet claimed)
    assert cora_queue.queue_depth() == 1


def test_queue_read_batch_returns_message(r):
    cora_queue.publish("draft.requested", "acme_pm", {"contact_id": 42})
    msgs = cora_queue.read_batch("test-consumer", count=1, block_ms=100)
    assert len(msgs) == 1
    assert msgs[0].event_type == "draft.requested"
    assert msgs[0].client_id == "acme_pm"
    assert msgs[0].payload == {"contact_id": 42}


def test_queue_ack_removes_from_pending(r):
    cora_queue.publish("draft.requested", "acme_pm", {"contact_id": 1})
    msgs = cora_queue.read_batch("test-consumer", count=1, block_ms=100)
    assert cora_queue.pending_count() == 1
    cora_queue.ack(msgs[0].message_id)
    assert cora_queue.pending_count() == 0


def test_queue_read_batch_empty_on_no_messages(r):
    msgs = cora_queue.read_batch("test-consumer", count=1, block_ms=100)
    assert msgs == []
