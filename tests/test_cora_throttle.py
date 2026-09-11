"""Unit tests for Cora throttle and kill-switch.

Uses fakeredis — no live Redis or DB required. Covers:
- approval_pending_count() reads the Redis counter
- notify_draft_queued() increments counter and enforces capacity
- notify_approval_resolved() decrements counter and lifts auto-pause
- Hysteresis: auto-pause stays until count < RESUME_THRESHOLD, not just < CAPACITY
- is_auto_paused() fails open (False) on Redis error
- cora_should_stop() fires on Relay halt or auto-pause
- Queue publish/pending_count integration with fakeredis
- S-2 (W0 §3.0.2 C): the 24-hour stale-draft age trigger — a live check
  independent of the count-based sticky flag and its hysteresis
"""

import itertools
import time
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

_id_counter = itertools.count()


def _new_id() -> str:
    """A fresh, unique draft/work-order id — PR #50's finding 7 fix keys
    the age-tracking ZSET by id rather than FIFO position, so tests that
    only care about the COUNTER (not which specific entry ages out) can
    use a throwaway unique id per call and never collide."""
    return f"draft-{next(_id_counter)}"

from src.agents.cora import throttle
from src.agents.cora.throttle import (
    DRAFT_MAX_AGE_HOURS,
    DRAFT_QUEUE_CAPACITY,
    RESUME_THRESHOLD,
    _AUTO_PAUSED_KEY,
    _PENDING_KEY,
    _QUEUED_AT_KEY,
    approval_pending_count,
    is_auto_paused,
    notify_approval_resolved,
    notify_draft_queued,
    pause_reason,
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
    assert notify_draft_queued(_new_id()) == 1
    assert notify_draft_queued(_new_id()) == 2
    assert approval_pending_count() == 2


def test_notify_draft_queued_sets_auto_pause_at_capacity(r):
    for _ in range(DRAFT_QUEUE_CAPACITY - 1):
        notify_draft_queued(_new_id())
    assert not is_auto_paused()  # one below capacity — not yet paused
    notify_draft_queued(_new_id())        # hits capacity
    assert is_auto_paused()


def test_notify_draft_queued_does_not_double_set_auto_pause(r):
    for _ in range(DRAFT_QUEUE_CAPACITY + 5):
        notify_draft_queued(_new_id())
    # auto_paused key should exist exactly once (SET idempotent anyway but logic is clean)
    assert is_auto_paused()
    assert approval_pending_count() == DRAFT_QUEUE_CAPACITY + 5


# ── notify_approval_resolved ──────────────────────────────────────────────────

def test_notify_approval_resolved_decrements_counter(r):
    r.set(_PENDING_KEY, "10")
    assert notify_approval_resolved(_new_id()) == 9
    assert approval_pending_count() == 9


def test_notify_approval_resolved_floors_at_zero(r):
    r.set(_PENDING_KEY, "0")
    result = notify_approval_resolved(_new_id())
    assert result == 0
    assert approval_pending_count() == 0


def test_notify_approval_resolved_lifts_auto_pause_below_resume_threshold(r):
    # Simulate being at capacity with auto-pause set
    r.set(_PENDING_KEY, str(RESUME_THRESHOLD))
    r.set(_AUTO_PAUSED_KEY, "1")
    # One more resolution drops below RESUME_THRESHOLD
    notify_approval_resolved(_new_id())
    assert not is_auto_paused()


# ── Hysteresis ────────────────────────────────────────────────────────────────

def test_auto_pause_not_lifted_between_threshold_and_capacity(r):
    """Auto-pause stays active when count is between RESUME_THRESHOLD and CAPACITY."""
    # Start at capacity
    for _ in range(DRAFT_QUEUE_CAPACITY):
        notify_draft_queued(_new_id())
    assert is_auto_paused()

    # Drain down to just above RESUME_THRESHOLD — should still be paused
    target = RESUME_THRESHOLD + 1
    while approval_pending_count() > target:
        notify_approval_resolved(_new_id())

    assert is_auto_paused(), (
        f"Auto-pause should persist at count={approval_pending_count()}, "
        f"which is above RESUME_THRESHOLD={RESUME_THRESHOLD}"
    )


def test_auto_pause_lifts_exactly_at_resume_threshold(r):
    """Auto-pause clears when count drops to RESUME_THRESHOLD - 1."""
    for _ in range(DRAFT_QUEUE_CAPACITY):
        notify_draft_queued(_new_id())
    # Drain to exactly RESUME_THRESHOLD (still paused)
    while approval_pending_count() > RESUME_THRESHOLD:
        notify_approval_resolved(_new_id())
    assert is_auto_paused()

    # One more → drops below RESUME_THRESHOLD → resumes
    notify_approval_resolved(_new_id())
    assert not is_auto_paused()


# ── S-2: 24-hour stale-draft age trigger ──────────────────────────────────────

def test_notify_draft_queued_pushes_timestamp_onto_age_zset(r):
    notify_draft_queued(_new_id())
    assert r.zcard(_QUEUED_AT_KEY) == 1


def test_notify_approval_resolved_removes_the_matching_draft_only(r):
    """PR #50 finding 7: removal is now by identity, not FIFO position —
    resolving draft A must not touch draft B's still-outstanding entry."""
    draft_a, draft_b = _new_id(), _new_id()
    notify_draft_queued(draft_a)
    notify_draft_queued(draft_b)
    assert r.zcard(_QUEUED_AT_KEY) == 2
    notify_approval_resolved(draft_a)
    assert r.zcard(_QUEUED_AT_KEY) == 1
    assert r.zscore(_QUEUED_AT_KEY, draft_b) is not None
    assert r.zscore(_QUEUED_AT_KEY, draft_a) is None


def test_notify_approval_resolved_for_unrelated_id_does_not_touch_a_real_entry(r):
    """The exact bug this finding closes: a generic, non-Cora work-order
    decision calling notify_approval_resolved(some_other_order_id) must
    never remove a still-unreviewed Cora draft's entry. ZREM on an id
    never ZADDed by Cora's own worker is a safe, targeted no-op."""
    cora_draft = _new_id()
    notify_draft_queued(cora_draft)
    assert r.zcard(_QUEUED_AT_KEY) == 1

    unrelated_order_id = _new_id()
    notify_approval_resolved(unrelated_order_id)

    assert r.zcard(_QUEUED_AT_KEY) == 1
    assert r.zscore(_QUEUED_AT_KEY, cora_draft) is not None


def test_notify_approval_resolved_pop_on_empty_age_zset_is_a_safe_noop(r):
    # No notify_draft_queued() call — the ZSET is empty. A generic
    # (non-Cora) work-order decision can still call notify_approval_resolved()
    # today (see throttle.py's notify_approval_resolved docstring) — ZREM on
    # an empty/nonexistent key must not raise.
    result = notify_approval_resolved(_new_id())
    assert result == 0


def test_is_auto_paused_true_when_oldest_item_older_than_24h(r):
    """Count stays at 1 — nowhere near DRAFT_QUEUE_CAPACITY — proving the age
    trigger fires independently of the count trigger."""
    stale_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 60)
    r.zadd(_QUEUED_AT_KEY, {_new_id(): stale_ts})
    r.set(_PENDING_KEY, "1")
    assert is_auto_paused() is True


def test_is_auto_paused_false_when_oldest_item_well_within_24h(r):
    fresh_ts = time.time() - 3600  # 1 hour old
    r.zadd(_QUEUED_AT_KEY, {_new_id(): fresh_ts})
    r.set(_PENDING_KEY, "1")
    assert is_auto_paused() is False


def test_exactly_24h_old_is_not_paused(r):
    """Boundary: 'older than 24 hours' is a strict inequality — exactly
    24h00m00s is not yet a pause condition. Pins throttle.time.time() to a
    fixed value for both the setup and the check — using two independent
    real time.time() calls would leave a few microseconds of wall-clock
    drift between them, occasionally pushing age just past the boundary
    and making this exact-boundary test flaky."""
    fixed_now = time.time()
    boundary_ts = fixed_now - (DRAFT_MAX_AGE_HOURS * 3600)
    r.zadd(_QUEUED_AT_KEY, {_new_id(): boundary_ts})
    with patch("src.agents.cora.throttle.time.time", return_value=fixed_now):
        assert is_auto_paused() is False


def test_24h_plus_one_second_is_paused(r):
    just_over_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 1)
    r.zadd(_QUEUED_AT_KEY, {_new_id(): just_over_ts})
    assert is_auto_paused() is True


def test_age_pause_clears_after_stale_item_is_resolved(r):
    stale_id = _new_id()
    stale_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 60)
    r.zadd(_QUEUED_AT_KEY, {stale_id: stale_ts})
    assert is_auto_paused() is True

    notify_approval_resolved(stale_id)  # removes the stale entry, by id
    assert is_auto_paused() is False


def test_multiple_stale_entries_keep_age_pause_active_until_all_resolved(r):
    """With identity-based removal, resolving one stale entry has no effect
    on the others — the age condition persists until every stale entry
    still outstanding is individually resolved. Realistic for a queue
    that's been badly behind for a while, not just one old outlier."""
    stale_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 60)
    stale_ids = [_new_id() for _ in range(3)]
    for sid in stale_ids:
        r.zadd(_QUEUED_AT_KEY, {sid: stale_ts})
    assert is_auto_paused() is True

    notify_approval_resolved(stale_ids[0])
    assert is_auto_paused() is True

    notify_approval_resolved(stale_ids[1])
    assert is_auto_paused() is True

    notify_approval_resolved(stale_ids[2])  # last one — ZSET now empty
    assert is_auto_paused() is False


def test_resume_blocked_by_remaining_stale_items_even_after_count_flag_clears(r):
    """A count-triggered pause must NOT lift while stale items remain, even
    once the count itself drops below RESUME_THRESHOLD — proving
    is_auto_paused()'s OR logic, not _maybe_resume() itself, is what keeps
    the platform paused in this case. Only the FRESH entries are resolved
    here — the stale ones are never touched, so (unlike the old FIFO-pop
    design) they cannot accidentally clear early."""
    stale_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 60)
    resolves_to_clear_count = DRAFT_QUEUE_CAPACITY - RESUME_THRESHOLD + 1  # 11
    num_stale = 4

    stale_ids = [_new_id() for _ in range(num_stale)]
    for sid in stale_ids:
        r.zadd(_QUEUED_AT_KEY, {sid: stale_ts})
    r.set(_PENDING_KEY, str(num_stale))  # counter reflects the pre-seeded stale entries
    fresh_ids = []
    for _ in range(DRAFT_QUEUE_CAPACITY - num_stale):
        fid = _new_id()
        fresh_ids.append(fid)
        notify_draft_queued(fid)  # fresh entries, fills to capacity, sets count flag
    assert throttle._count_flag_set() is True
    assert is_auto_paused() is True

    for fid in fresh_ids[:resolves_to_clear_count]:
        notify_approval_resolved(fid)

    # Count-based sticky flag has cleared...
    assert throttle._count_flag_set() is False
    # ...but the stale entries were never resolved, so is_auto_paused()
    # must still be True.
    assert is_auto_paused() is True


def test_oldest_queued_age_returns_none_on_redis_error():
    broken = MagicMock()
    broken.zrange.side_effect = RuntimeError("Redis down")
    broken.exists.return_value = False  # count flag not set
    with patch("src.agents.cora.throttle.get_redis_client", return_value=broken):
        assert is_auto_paused() is False


# ── pause_reason — code-review fix: distinguish count vs age in logs ────────

def test_pause_reason_none_when_healthy(r):
    assert pause_reason() is None


def test_pause_reason_count_only(r):
    for _ in range(DRAFT_QUEUE_CAPACITY):
        notify_draft_queued(_new_id())
    assert pause_reason() == "count"


def test_pause_reason_age_only(r):
    stale_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 60)
    r.zadd(_QUEUED_AT_KEY, {_new_id(): stale_ts})
    assert pause_reason() == "age"


def test_pause_reason_both(r):
    stale_ts = time.time() - (DRAFT_MAX_AGE_HOURS * 3600 + 60)
    r.zadd(_QUEUED_AT_KEY, {_new_id(): stale_ts})
    r.set(_PENDING_KEY, str(DRAFT_QUEUE_CAPACITY))
    r.set(_AUTO_PAUSED_KEY, "1")
    assert pause_reason() == "count+age"


# ── Atomicity — code-review fix: INCR+ZADD / DECR+ZREM via pipeline ─────────

def test_notify_draft_queued_counter_and_zset_stay_in_lockstep(r):
    """The pipeline fix means these two can never observably disagree —
    regression guard for the crash-window finding."""
    for _ in range(5):
        notify_draft_queued(_new_id())
    assert approval_pending_count() == r.zcard(_QUEUED_AT_KEY) == 5


def test_notify_approval_resolved_counter_and_zset_stay_in_lockstep(r):
    ids = [_new_id() for _ in range(5)]
    for did in ids:
        notify_draft_queued(did)
    for did in ids[:3]:
        notify_approval_resolved(did)
    assert approval_pending_count() == r.zcard(_QUEUED_AT_KEY) == 2


def test_notify_draft_queued_pipeline_failure_leaves_neither_write_applied(r):
    """If the pipeline itself fails, INCR must not land without its paired
    ZADD (the exact crash-window bug this fix closes) — verified by
    forcing pipe.execute() to raise and confirming the counter never moved."""

    class _FailingPipeline:
        def incr(self, *a, **kw):
            return self

        def zadd(self, *a, **kw):
            return self

        def execute(self):
            raise RuntimeError("pipeline execute failed")

    broken = MagicMock()
    broken.pipeline.return_value = _FailingPipeline()
    with patch("src.agents.cora.throttle.get_redis_client", return_value=broken):
        result = notify_draft_queued(_new_id())
    assert result == -1
    assert approval_pending_count() == 0  # unaffected — nothing else touched real_client


# ── is_auto_paused — fail open ────────────────────────────────────────────────

def test_is_auto_paused_returns_false_on_redis_error():
    """S-2 update: is_auto_paused() now also reads the age-tracking ZSET
    (_oldest_queued_age_seconds() -> zrange), so a fully-broken Redis must
    fail both reads, not just .exists()."""
    broken = MagicMock()
    broken.exists.side_effect = RuntimeError("Redis down")
    broken.zrange.side_effect = RuntimeError("Redis down")
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
