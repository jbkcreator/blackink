"""Regression tests for two queue bugs fixed in Week 0.

Issue 1 — CLIENT halt dead-letters valid deferred messages
  (worker.py:87-99, queue.py:128-180):
  claim_stale() was reclaiming and eventually dead-lettering messages for
  clients with an active halt. After the fix, halted-client messages are
  skipped by the sweep — their times_delivered count stays frozen and they
  are never moved to the DLQ while the halt is active.

Issue 2 — Stream retention silently drops unread draft requests
  (queue.py:60-77):
  publish() was calling xadd with maxlen=STREAM_MAXLEN. Auto-trimming a
  work queue evicts entries the consumer group has not yet read. After the
  fix, publish() does not pass maxlen to xadd.

All tests use fakeredis — no live Redis or DB required.
"""
from __future__ import annotations

from unittest.mock import patch

import fakeredis
import pytest

from src.agents.cora import queue as cora_queue
from src.agents.cora.queue import (
    DLQ_KEY,
    GROUP_NAME,
    MAX_DELIVERIES,
    STREAM_KEY,
    STREAM_MAXLEN,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def r():
    """Fresh fakeredis instance patched into the queue module."""
    fake = fakeredis.FakeRedis(decode_responses=True)
    with patch("src.agents.cora.queue.get_redis_client", return_value=fake):
        yield fake


def _publish_and_claim(r, client_id: str = "acme_pm") -> cora_queue.DraftMessage:
    """Publish one message and claim it so it lands in the PEL (times_delivered=1)."""
    cora_queue.publish("draft.requested", client_id, {"contact_id": 1})
    msgs = cora_queue.read_batch("test-consumer", count=1, block_ms=100)
    assert len(msgs) == 1
    return msgs[0]


def _delivery_count(r, message_id: str) -> int:
    """Read the current times_delivered for a PEL entry directly from Redis."""
    entries = r.xpending_range(STREAM_KEY, GROUP_NAME, min="-", max="+", count=100)
    for e in entries:
        if e["message_id"] == message_id:
            return e.get("times_delivered", 1)
    return 0


# ── Issue 1: CLIENT halt must not cause dead-lettering ────────────────────────

class TestClientHaltDeadLetter:
    """Regression: claim_stale() must not reclaim or dead-letter messages
    while their client's halt is active.

    The sweep runs every ~60 s; MAX_DELIVERIES × 60 s ≈ 3 minutes of halt
    previously resulted in permanent message loss. The fix skips any PEL
    entry whose client currently has an active halt.
    """

    def test_message_survives_beyond_max_deliveries_sweeps(self, r):
        """Core regression: MAX_DELIVERIES + 1 sweeps under a CLIENT halt
        must leave the message in the PEL and the DLQ empty."""
        msg = _publish_and_claim(r)

        with patch("src.agents.cora.queue.is_halted", return_value=True):
            for _ in range(MAX_DELIVERIES + 1):
                reclaimed = cora_queue.claim_stale("sweeper", min_idle_ms=0)

        assert cora_queue.pending_count() == 1, "message must remain pending while halted"
        assert r.xlen(DLQ_KEY) == 0, "halted-client message must never reach the DLQ"
        assert reclaimed == []

    def test_delivery_count_stays_frozen_during_halt(self, r):
        """times_delivered must not increment while the client is halted.

        The message must not consume its retry budget during a legitimate
        operational pause — xclaim must never be called for halted entries.
        """
        msg = _publish_and_claim(r)
        assert _delivery_count(r, msg.message_id) == 1

        with patch("src.agents.cora.queue.is_halted", return_value=True):
            for _ in range(MAX_DELIVERIES + 1):
                cora_queue.claim_stale("sweeper", min_idle_ms=0)

        assert _delivery_count(r, msg.message_id) == 1, (
            "times_delivered must not increase for a halted client's message"
        )

    def test_message_is_fully_recoverable_after_halt_is_lifted(self, r):
        """After the halt is lifted, claim_stale() must reclaim the deferred
        message and allow it to be acked normally."""
        msg = _publish_and_claim(r)

        # Multiple sweeps under halt — message stays put.
        with patch("src.agents.cora.queue.is_halted", return_value=True):
            for _ in range(MAX_DELIVERIES):
                cora_queue.claim_stale("sweeper", min_idle_ms=0)

        assert cora_queue.pending_count() == 1

        # Lift halt — next sweep must return the message.
        with patch("src.agents.cora.queue.is_halted", return_value=False):
            reclaimed = cora_queue.claim_stale("sweeper", min_idle_ms=0)

        assert len(reclaimed) == 1
        assert reclaimed[0].message_id == msg.message_id
        assert reclaimed[0].client_id == "acme_pm"

        # Ack it — queue drains to zero, confirming full end-to-end recovery.
        cora_queue.ack(reclaimed[0].message_id)
        assert cora_queue.pending_count() == 0

    def test_genuine_failures_still_dead_letter_when_not_halted(self, r):
        """Guard: halted-client protection must not shield genuinely failed
        messages. A non-halted client's message must still reach the DLQ
        after MAX_DELIVERIES sweeps."""
        _publish_and_claim(r, client_id="acme_pm")

        with patch("src.agents.cora.queue.is_halted", return_value=False):
            for _ in range(MAX_DELIVERIES + 1):
                cora_queue.claim_stale("sweeper", min_idle_ms=0)

        assert r.xlen(DLQ_KEY) == 1, "genuinely failed message must reach the DLQ"
        assert cora_queue.pending_count() == 0

    def test_selective_halt_only_freezes_the_halted_client(self, r):
        """When client A is halted and client B is not, only client A's
        message is frozen. Client B's message proceeds through normal
        dead-lettering, unaffected."""
        _publish_and_claim(r, client_id="halted_client")
        _publish_and_claim(r, client_id="healthy_client")

        def _selective_halt(client_id: str | None = None, **_kw) -> bool:
            return client_id == "halted_client"

        with patch("src.agents.cora.queue.is_halted", side_effect=_selective_halt):
            for _ in range(MAX_DELIVERIES + 1):
                cora_queue.claim_stale("sweeper", min_idle_ms=0)

        # halted_client's message still pending; healthy_client's dead-lettered.
        assert cora_queue.pending_count() == 1
        assert r.xlen(DLQ_KEY) == 1

        dlq_entries = r.xrange(DLQ_KEY)
        _, dlq_fields = dlq_entries[0]
        assert dlq_fields.get("client_id") == "healthy_client", (
            "only the non-halted client's message should be in the DLQ"
        )


# ── Issue 2: publish() must not trim the work queue ───────────────────────────

class TestPublishNoTrim:
    """Regression: publish() must not pass maxlen to xadd.

    Auto-trimming a work queue at publish time silently evicts entries the
    consumer group has not yet read. Those entries are never claimed,
    acked, or dead-lettered — they disappear without any failure record.
    """

    def test_xadd_is_called_without_maxlen(self, r):
        """Direct regression: capture the xadd invocation and assert that
        maxlen is not present in the keyword arguments."""
        original_xadd = r.xadd
        captured_kwargs: list[dict] = []

        def _spy(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return original_xadd(*args, **kwargs)

        r.xadd = _spy
        cora_queue.publish("draft.requested", "acme_pm", {"contact_id": 1})

        assert len(captured_kwargs) == 1
        assert "maxlen" not in captured_kwargs[0], (
            "publish() must not pass maxlen to xadd — "
            "trimming a work queue evicts unconsumed entries"
        )

    def test_entries_are_not_evicted_during_consumer_downtime(self, r):
        """Behavioral regression: messages published while no consumer is
        active must all survive in the stream."""
        count = 25  # enough to prove no silent eviction; well below old maxlen

        for i in range(count):
            cora_queue.publish("draft.requested", "acme_pm", {"contact_id": i})

        assert cora_queue.queue_depth() == count, (
            f"expected {count} entries in the stream but got "
            f"{cora_queue.queue_depth()} — publish() must not trim the queue"
        )

    def test_all_published_messages_are_consumable_after_downtime(self, r):
        """End-to-end: every message published while consumers were stopped
        must be delivered exactly once when a consumer starts reading."""
        count = 20

        for i in range(count):
            cora_queue.publish("draft.requested", "acme_pm", {"contact_id": i})

        received: list[cora_queue.DraftMessage] = []
        while True:
            batch = cora_queue.read_batch("test-consumer", count=10, block_ms=50)
            if not batch:
                break
            received.extend(batch)
            for msg in batch:
                cora_queue.ack(msg.message_id)

        assert len(received) == count, (
            f"expected all {count} published messages to be consumable "
            f"but received only {len(received)}"
        )
        assert cora_queue.pending_count() == 0

    def test_idempotency_key_preserved_across_publish_read_cycle(self, r):
        """Smoke-check that the publish→read pipeline is intact after the
        xadd signature change (no accidental field drops)."""
        idem_key = "test-idempotency-key-abc123"
        cora_queue.publish(
            "draft.requested", "acme_pm", {"contact_id": 9}, idempotency_key=idem_key
        )
        msgs = cora_queue.read_batch("test-consumer", count=1, block_ms=100)

        assert len(msgs) == 1
        assert msgs[0].idempotency_key == idem_key
        assert msgs[0].event_type == "draft.requested"
        assert msgs[0].client_id == "acme_pm"
        assert msgs[0].payload == {"contact_id": 9}
