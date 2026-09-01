"""Cora draft-generation worker loop.

Reads draft.requested events from the Redis Stream, checks halt/throttle
state before claiming work, and hands each event to _process_draft().

Control flow per iteration:
  1. cora_should_stop(client_id) — Relay halt or approval-backlog auto-pause:
     idle, do NOT claim new events, loop back.
  2. _maybe_resume() — check if auto-pause can lift (backlog drained) even
     while idle. Called on every idle tick so the worker self-heals as soon as
     Slack approvals clear the backlog, without waiting for an external signal.
  3. read_batch() — claim one event from the stream.
  4. _process_draft() — generate draft, post to Slack, notify_draft_queued().
  5. ack() — only after Slack post succeeds (durable side effect happened).

Stale-message sweep runs every CLAIM_SWEEP_EVERY_N_LOOPS iterations and
reclaims events that were claimed but never acked (worker crash mid-flight).

SIGINT/SIGTERM trigger graceful shutdown: stop claiming new events, let the
in-flight message finish, then exit. An unacked in-flight message is picked up
by claim_stale() on the next worker restart.

_process_draft() is intentionally minimal for week 0 — the actual LLM draft
generation and Slack posting belong to the outreach-pipeline workstream (not
Dev 2 scope). It logs the event and calls notify_draft_queued() as a
placeholder, which is enough to exercise the throttle path end-to-end.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from typing import Any, Dict, Optional

from src.agents.cora import kill_switch, queue
from src.agents.cora.throttle import _maybe_resume, notify_draft_queued, approval_pending_count

logger = logging.getLogger(__name__)

CLAIM_MIN_IDLE_MS = 60_000
CLAIM_SWEEP_EVERY_N_LOOPS = 12
IDLE_SLEEP_SECONDS = 5


def _consumer_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _process_draft(msg: "queue.DraftMessage") -> None:
    """Placeholder: log the event and increment the approval backlog counter.

    Week 0 scope: the actual LLM draft generation and Slack posting will be
    implemented in the outreach-pipeline workstream. The throttle integration
    (notify_draft_queued) is exercised here so the backlog counter is accurate
    even before the full pipeline is wired up.
    """
    logger.info(
        "cora.worker: processing draft event event_type=%s client_id=%s message_id=%s",
        msg.event_type, msg.client_id, msg.message_id,
    )
    count = notify_draft_queued()
    logger.info(
        "cora.worker: draft queued for approval — approval_pending=%d", count
    )


class Worker:
    def __init__(self, consumer_name: Optional[str] = None) -> None:
        self.consumer_name = consumer_name or _consumer_name()
        self._stop = False
        self._loop_count = 0

    def request_stop(self, *_args: Any) -> None:
        logger.info(
            "cora.worker: shutdown requested (consumer=%s) — finishing in-flight work",
            self.consumer_name,
        )
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def _process_one(self, msg: "queue.DraftMessage") -> None:
        # Per-message CLIENT halt check — the pre-loop check in run_forever()
        # only evaluates GLOBAL halts because no message has been claimed yet.
        # Once we hold a message we know its client, so re-check here before
        # doing any work. Leave unacked so the message is recoverable via
        # claim_stale() after the halt is lifted.
        if kill_switch.cora_should_stop(client_id=msg.client_id):
            logger.warning(
                "cora.worker: CLIENT halt active for client_id=%s — "
                "leaving message_id=%s unacked for recovery",
                msg.client_id, msg.message_id,
            )
            return
        try:
            _process_draft(msg)
            queue.ack(msg.message_id)
        except Exception:
            logger.exception(
                "cora.worker: _process_draft raised for message_id=%s event_type=%s "
                "delivery_count=%d — leaving unacked for claim_stale()",
                msg.message_id, msg.event_type, msg.delivery_count,
            )

    def _sweep_stale(self) -> None:
        reclaimed = queue.claim_stale(self.consumer_name, min_idle_ms=CLAIM_MIN_IDLE_MS)
        for msg in reclaimed:
            logger.info(
                "cora.worker: reclaimed stale message_id=%s event_type=%s delivery_count=%d",
                msg.message_id, msg.event_type, msg.delivery_count,
            )
            self._process_one(msg)

    def run_forever(self, block_ms: int = 1000) -> None:
        queue.ensure_group()
        logger.info("cora.worker: starting (consumer=%s)", self.consumer_name)

        while not self._stop:
            self._loop_count += 1

            # Check halt / throttle before claiming any work.
            if kill_switch.cora_should_stop():
                # While idling, still check for throttle self-resume so the
                # worker wakes up as soon as the backlog clears without needing
                # an explicit external signal.
                _maybe_resume(count=approval_pending_count())
                time.sleep(IDLE_SLEEP_SECONDS)
                continue

            try:
                if self._loop_count % CLAIM_SWEEP_EVERY_N_LOOPS == 0:
                    self._sweep_stale()

                messages = queue.read_batch(
                    self.consumer_name, count=1, block_ms=block_ms
                )
                for msg in messages:
                    if self._stop:
                        break
                    self._process_one(msg)

            except Exception:
                logger.exception("cora.worker: main loop iteration failed — continuing")

        logger.info("cora.worker: stopped (consumer=%s)", self.consumer_name)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = Worker()
    worker.install_signal_handlers()
    worker.run_forever()


if __name__ == "__main__":
    main()
