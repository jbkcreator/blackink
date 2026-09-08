"""Tests for Redis-outage resilience in the respond worker and queue.

Covers:
  - publish() returns None (never raises) when ensure_group raises
  - publish() returns None (never raises) when xadd raises
  - read_batch() returns [] (never raises) when ensure_group raises
  - Worker.run_forever() survives Redis down at startup and recovers
  - Worker._group_confirmed is reset after a loop exception
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, call, patch

import pytest

from src.agents.respond import queue as respond_queue
from src.agents.respond.worker import Worker


# ---------------------------------------------------------------------------
# publish() — should always return None on any Redis failure, never raise
# ---------------------------------------------------------------------------

class TestPublishRedisFailure:
    def test_returns_none_when_ensure_group_raises(self):
        with patch("src.agents.respond.queue.ensure_group", side_effect=ConnectionError("Redis down")):
            result = respond_queue.publish(db_id=1, client_id="CL1", idempotency_key="k1")
        assert result is None

    def test_returns_none_when_xadd_raises(self):
        with patch("src.agents.respond.queue.ensure_group"), \
             patch("src.agents.respond.queue.get_redis_client") as mock_r:
            mock_r.return_value.xadd.side_effect = ConnectionError("Redis down")
            result = respond_queue.publish(db_id=2, client_id="CL1", idempotency_key="k2")
        assert result is None

    def test_does_not_raise_on_redis_failure(self):
        with patch("src.agents.respond.queue.ensure_group", side_effect=OSError("timeout")):
            # Must complete without raising
            respond_queue.publish(db_id=3, client_id="CL1", idempotency_key="k3")

    def test_returns_stream_id_on_success(self):
        with patch("src.agents.respond.queue.ensure_group"), \
             patch("src.agents.respond.queue.get_redis_client") as mock_r:
            mock_r.return_value.xadd.return_value = "1234-0"
            result = respond_queue.publish(db_id=4, client_id="CL1", idempotency_key="k4")
        assert result == "1234-0"


# ---------------------------------------------------------------------------
# read_batch() — should always return [] on any Redis failure, never raise
# ---------------------------------------------------------------------------

class TestReadBatchRedisFailure:
    def test_returns_empty_when_ensure_group_raises(self):
        with patch("src.agents.respond.queue.ensure_group", side_effect=ConnectionError("Redis down")):
            result = respond_queue.read_batch("consumer-1")
        assert result == []

    def test_returns_empty_when_xreadgroup_raises(self):
        with patch("src.agents.respond.queue.ensure_group"), \
             patch("src.agents.respond.queue.get_redis_client") as mock_r:
            mock_r.return_value.xreadgroup.side_effect = ConnectionError("Redis down")
            result = respond_queue.read_batch("consumer-1")
        assert result == []

    def test_does_not_raise_on_redis_failure(self):
        with patch("src.agents.respond.queue.ensure_group", side_effect=OSError("timeout")):
            respond_queue.read_batch("consumer-1")


# ---------------------------------------------------------------------------
# Worker.run_forever() — startup recovery
# ---------------------------------------------------------------------------

class TestWorkerRedisRecovery:
    def _make_worker(self) -> Worker:
        return Worker(consumer_name="test-consumer")

    def test_group_confirmed_false_at_init(self):
        w = self._make_worker()
        assert w._group_confirmed is False

    def test_group_confirmed_true_after_successful_ensure(self):
        w = self._make_worker()
        call_count = 0

        def fake_ensure():
            nonlocal call_count
            call_count += 1

        with patch("src.agents.respond.worker.queue.ensure_group", side_effect=fake_ensure), \
             patch("src.agents.respond.worker.queue.read_batch", return_value=[]) as mock_rb:
            # Let the loop run once: ensure_group succeeds, read_batch returns []
            # then stop
            def stop_after_first_read(*args, **kwargs):
                w._stop = True
                return []

            mock_rb.side_effect = stop_after_first_read
            w.run_forever(block_ms=0)

        assert w._group_confirmed is True
        assert call_count == 1  # ensure_group called exactly once

    def test_worker_survives_redis_down_at_startup(self):
        """First ensure_group raises (Redis down), second succeeds; worker must
        not exit — it must process normally after Redis recovers."""
        w = self._make_worker()
        ensure_calls = []

        def flaky_ensure():
            ensure_calls.append(1)
            if len(ensure_calls) == 1:
                raise ConnectionError("Redis not yet available")
            # Second call succeeds

        read_batch_calls = []

        def stop_after_first_read(*args, **kwargs):
            read_batch_calls.append(1)
            w._stop = True
            return []

        with patch("src.agents.respond.worker.queue.ensure_group", side_effect=flaky_ensure), \
             patch("src.agents.respond.worker.queue.read_batch", side_effect=stop_after_first_read), \
             patch("src.agents.respond.worker.time.sleep"):  # skip 5s backoff
            w.run_forever(block_ms=0)

        assert len(ensure_calls) == 2, "ensure_group must be retried after failure"
        assert len(read_batch_calls) == 1, "Worker must reach read_batch after recovery"
        assert w._group_confirmed is True

    def test_group_confirmed_reset_after_loop_exception(self):
        """An exception inside the loop (other than ensure_group) resets
        _group_confirmed so the next iteration re-verifies Redis is reachable."""
        w = self._make_worker()
        iteration = 0

        def flaky_read_batch(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration == 1:
                raise RuntimeError("unexpected failure")
            w._stop = True
            return []

        with patch("src.agents.respond.worker.queue.ensure_group"), \
             patch("src.agents.respond.worker.queue.read_batch", side_effect=flaky_read_batch), \
             patch("src.agents.respond.worker.time.sleep"):
            w.run_forever(block_ms=0)

        # After the RuntimeError, _group_confirmed was reset to False and then
        # set to True again by the second successful ensure_group call.
        assert w._group_confirmed is True

    def test_ensure_group_not_called_again_when_already_confirmed(self):
        """Once _group_confirmed=True, ensure_group is not called on subsequent
        iterations (guard prevents repeated Redis round-trips)."""
        w = self._make_worker()
        ensure_call_count = 0
        iteration = 0

        def counting_ensure():
            nonlocal ensure_call_count
            ensure_call_count += 1

        def multi_iter_read(*args, **kwargs):
            nonlocal iteration
            iteration += 1
            if iteration >= 3:
                w._stop = True
            return []

        with patch("src.agents.respond.worker.queue.ensure_group", side_effect=counting_ensure), \
             patch("src.agents.respond.worker.queue.read_batch", side_effect=multi_iter_read):
            w.run_forever(block_ms=0)

        assert iteration == 3
        assert ensure_call_count == 1  # called once, then guarded
