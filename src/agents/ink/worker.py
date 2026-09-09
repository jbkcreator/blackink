"""Ink (Campaign Agent) — worker process.

Consumes ink:work_orders, runs the LangGraph campaign graph per work order.
One work order = one graph thread (thread_id = work_order_id).

The graph suspends twice (wait_reply, wait_approve). Resume signals arrive
via ink:resume_signals — a separate lightweight stream that IMAP listener
and Slack approval webhook write to.

Usage
─────
    python -m src.agents.ink.worker

Environment
───────────
    Requires DATABASE_URL for the Postgres checkpointer.
    Requires REDIS_URL for the ink:work_orders stream.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import time
from typing import Optional

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

from src.agents.ink.graph import build_graph
from src.agents.ink.queue import ack, claim_stale, ensure_group, read_batch
from src.agents.ink.state import GlobalState, InkStage
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

CONSUMER_NAME         = f"ink-worker-{socket.gethostname()}-{os.getpid()}"
RESUME_STREAM_KEY     = "ink:resume_signals"
RESUME_GROUP_NAME     = "ink_resume_workers"
POLL_INTERVAL_MS      = 1_000
CLAIM_INTERVAL_SEC    = 60
CLAIM_MIN_IDLE_MS     = 60_000

# Nodes that suspend via interrupt() — resumption comes from ink:resume_signals,
# not from a fresh graph.invoke() call.
_INTERRUPT_NODES = frozenset({"wait_reply", "wait_approve"})


def _ensure_resume_group() -> None:
    r = get_redis_client()
    try:
        r.xgroup_create(RESUME_STREAM_KEY, RESUME_GROUP_NAME, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _read_resume_signals() -> list[dict]:
    """Poll ink:resume_signals for resume events — new messages AND own PEL.

    Reads own PEL first (id="0") to pick up any messages that were delivered
    to this consumer in a prior run but never acked (e.g. crash mid-process).
    Then reads new undelivered messages (id=">").
    """
    r = get_redis_client()
    signals = []
    for stream_id in ("0", ">"):
        try:
            # PEL re-delivery ("0"): non-blocking — an empty PEL must return
            # immediately so the worker proceeds to _process_new_work_orders().
            # New messages (">"): bounded wait handled by _process_new_work_orders'
            # own read_batch call, so stay non-blocking here too.
            result = r.xreadgroup(
                RESUME_GROUP_NAME, CONSUMER_NAME,
                {RESUME_STREAM_KEY: stream_id},
                count=10,
            )
        except Exception as exc:
            logger.debug("ink.worker: resume_signals read failed id=%s: %s", stream_id, exc)
            continue
        if not result:
            continue
        for _stream, entries in result:
            for message_id, fields in entries:
                if fields:  # xclaimed tombstones have None fields
                    signals.append({"_message_id": message_id, **fields})
    return signals


def _ack_resume_signal(message_id: str) -> None:
    r = get_redis_client()
    try:
        r.xack(RESUME_STREAM_KEY, RESUME_GROUP_NAME, message_id)
        r.xdel(RESUME_STREAM_KEY, message_id)
    except Exception as exc:
        logger.warning("ink.worker: resume signal ack failed %s: %s", message_id, exc)


def _initial_state(msg) -> GlobalState:
    """Build the initial GlobalState from a WorkOrderMessage."""
    return GlobalState(
        company_id=msg.company_id,
        campaign_id=msg.campaign_id,
        work_order_id=msg.work_order_id,
        client_id=msg.client_id,
        stage=InkStage.GHOST_SHOPPER,
        ghost_result=None,
        submitted_at=None,
        latency_sec=None,
        loss_est=None,
        pdf_url=None,
        fee_stack_url=None,
        video_id=None,
        landing_url=None,
        gif_url=None,
        draft_message_id=None,
        error=None,
    )


class InkWorker:
    def __init__(self, graph) -> None:
        self._graph = graph
        self._running = True
        self._last_claim = 0.0

        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT,  self._handle_stop)

    def _handle_stop(self, signum, frame) -> None:
        logger.info("ink.worker: received signal %s — stopping", signum)
        self._running = False

    def _run_graph(self, work_order_id: str, initial_state: GlobalState) -> None:
        config = {"configurable": {"thread_id": work_order_id}}
        try:
            result = self._graph.invoke(initial_state, config=config)
            logger.info(
                "ink.worker: graph completed or suspended — work_order_id=%s stage=%s",
                work_order_id, result.get("stage") if result else "unknown",
            )
        except Exception as exc:
            logger.error(
                "ink.worker: graph error — work_order_id=%s: %s",
                work_order_id, exc, exc_info=True,
            )
            raise

    def _resume_graph(self, work_order_id: str, resume_data: dict) -> None:
        config = {"configurable": {"thread_id": work_order_id}}
        try:
            result = self._graph.invoke(Command(resume=resume_data), config=config)
            logger.info(
                "ink.worker: graph resumed — work_order_id=%s stage=%s",
                work_order_id, result.get("stage") if result else "unknown",
            )
        except Exception as exc:
            logger.error(
                "ink.worker: resume error — work_order_id=%s: %s",
                work_order_id, exc, exc_info=True,
            )
            raise

    def _process_new_work_orders(self) -> None:
        messages = read_batch(CONSUMER_NAME, count=1, block_ms=POLL_INTERVAL_MS)
        for msg in messages:
            logger.info(
                "ink.worker: new work_order — company_id=%s work_order_id=%s delivery=%d",
                msg.company_id, msg.work_order_id, msg.delivery_count,
            )
            try:
                self._run_graph(msg.work_order_id, _initial_state(msg))
                ack(msg.message_id)
            except Exception:
                # Leave in PEL — claim_stale will retry or dead-letter
                logger.warning(
                    "ink.worker: leaving message in PEL for retry message_id=%s",
                    msg.message_id,
                )

    def _process_resume_signals(self) -> None:
        for signal in _read_resume_signals():
            message_id    = signal.pop("_message_id")
            work_order_id = signal.get("work_order_id", "")
            if not work_order_id:
                logger.warning("ink.worker: resume signal missing work_order_id — acking %s", message_id)
                _ack_resume_signal(message_id)
                continue

            resume_payload_raw = signal.get("resume_payload", "{}")
            try:
                resume_payload = json.loads(resume_payload_raw) if isinstance(resume_payload_raw, str) else resume_payload_raw
            except json.JSONDecodeError:
                logger.error(
                    "ink.worker: malformed resume_payload for work_order_id=%s — discarding",
                    work_order_id,
                )
                _ack_resume_signal(message_id)
                continue

            # Guard: only resume if the graph is still suspended at an interrupt node
            config = {"configurable": {"thread_id": work_order_id}}
            try:
                existing = self._graph.get_state(config)
            except Exception as exc:
                logger.warning(
                    "ink.worker: get_state failed work_order_id=%s: %s — discarding resume signal",
                    work_order_id, exc,
                )
                _ack_resume_signal(message_id)
                continue

            if not existing or not existing.next:
                logger.info(
                    "ink.worker: resume signal for completed/unknown work_order_id=%s — acking",
                    work_order_id,
                )
                _ack_resume_signal(message_id)
                continue

            logger.info(
                "ink.worker: processing resume — work_order_id=%s next=%s",
                work_order_id, list(existing.next),
            )
            try:
                self._resume_graph(work_order_id, resume_payload)
                _ack_resume_signal(message_id)
            except Exception:
                logger.warning(
                    "ink.worker: leaving resume signal in PEL message_id=%s",
                    message_id,
                )

    def _maybe_claim_stale_resume_signals(self) -> None:
        """Reclaim resume signals delivered to dead consumers back into this consumer.

        Resume signals use `>` in _read_resume_signals so they only receive new
        messages. Any signal delivered to a worker that died before acking it
        would be stuck in the PEL forever without this sweep.
        """
        now = time.monotonic()
        if now - self._last_claim < CLAIM_INTERVAL_SEC:
            return
        r = get_redis_client()
        try:
            pending = r.xpending_range(
                RESUME_STREAM_KEY, RESUME_GROUP_NAME, min="-", max="+", count=50
            )
        except Exception as exc:
            logger.warning("ink.worker: resume xpending_range failed: %s", exc)
            return
        stale = [
            e["message_id"]
            for e in pending
            if e.get("time_since_delivered", 0) >= CLAIM_MIN_IDLE_MS
               and e.get("consumer") != CONSUMER_NAME
        ]
        if not stale:
            return
        try:
            r.xclaim(
                RESUME_STREAM_KEY, RESUME_GROUP_NAME, CONSUMER_NAME,
                min_idle_time=CLAIM_MIN_IDLE_MS, message_ids=stale,
            )
            logger.info(
                "ink.worker: reclaimed %d stale resume signal(s) from dead consumers",
                len(stale),
            )
        except Exception as exc:
            logger.warning("ink.worker: resume xclaim failed: %s", exc)

    def _maybe_claim_stale(self) -> None:
        now = time.monotonic()
        if now - self._last_claim < CLAIM_INTERVAL_SEC:
            return
        self._last_claim = now
        stale = claim_stale(CONSUMER_NAME, min_idle_ms=CLAIM_MIN_IDLE_MS)
        for msg in stale:
            logger.info(
                "ink.worker: reclaimed stale — company_id=%s work_order_id=%s delivery=%d",
                msg.company_id, msg.work_order_id, msg.delivery_count,
            )
            config = {"configurable": {"thread_id": msg.work_order_id}}
            try:
                existing = self._graph.get_state(config)
            except Exception as exc:
                logger.warning(
                    "ink.worker: get_state failed work_order_id=%s: %s — starting fresh",
                    msg.work_order_id, exc,
                )
                existing = None

            try:
                if existing and existing.next:
                    if _INTERRUPT_NODES.intersection(existing.next):
                        # Suspended at wait_reply or wait_approve — external signal will resume it
                        logger.info(
                            "ink.worker: stale message at interrupt %s work_order_id=%s — acking",
                            list(existing.next), msg.work_order_id,
                        )
                    else:
                        # Crashed mid-node — resume from the last checkpoint
                        logger.info(
                            "ink.worker: stale mid-execution work_order_id=%s next=%s — resuming",
                            msg.work_order_id, list(existing.next),
                        )
                        self._graph.invoke(Command(resume=None), config=config)
                elif existing:
                    # Checkpoint exists but graph already completed — just ack
                    logger.info(
                        "ink.worker: stale already completed work_order_id=%s — acking",
                        msg.work_order_id,
                    )
                else:
                    # No checkpoint — graph never ran; start fresh
                    self._run_graph(msg.work_order_id, _initial_state(msg))
                ack(msg.message_id)
            except Exception:
                logger.warning(
                    "ink.worker: stale handling failed message_id=%s",
                    msg.message_id,
                )

    def _wait_for_redis(self, max_attempts: int = 10, base_delay: float = 2.0) -> None:
        """Exponential backoff until Redis is reachable. Raises after max_attempts."""
        for attempt in range(1, max_attempts + 1):
            try:
                ensure_group()
                _ensure_resume_group()
                return
            except Exception as exc:
                if attempt == max_attempts:
                    logger.error(
                        "ink.worker: Redis not reachable after %d attempts — giving up: %s",
                        max_attempts, exc,
                    )
                    raise
                delay = base_delay * (2 ** (attempt - 1))  # 2, 4, 8, 16 … seconds
                logger.warning(
                    "ink.worker: Redis not reachable (attempt %d/%d) — retrying in %.0fs: %s",
                    attempt, max_attempts, delay, exc,
                )
                time.sleep(delay)

    def run_forever(self, max_consecutive_errors: int = 10, error_backoff: float = 2.0) -> None:
        logger.info("ink.worker: starting — consumer=%s", CONSUMER_NAME)
        self._wait_for_redis()
        consecutive_errors = 0
        while self._running:
            try:
                self._process_resume_signals()
                self._process_new_work_orders()
                self._maybe_claim_stale_resume_signals()
                self._maybe_claim_stale()
                consecutive_errors = 0  # reset on any clean tick
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        "ink.worker: %d consecutive errors — shutting down: %s",
                        consecutive_errors, exc,
                    )
                    self._running = False
                    break
                delay = error_backoff * consecutive_errors
                logger.warning(
                    "ink.worker: loop error (%d/%d) — retrying in %.0fs: %s",
                    consecutive_errors, max_consecutive_errors, delay, exc,
                )
                time.sleep(delay)
        logger.info("ink.worker: stopped")


def _wait_for_postgres(
    database_url: str,
    max_attempts: int = 10,
    base_delay: float = 2.0,
) -> None:
    """Probe Postgres with exponential backoff until reachable. Raises after max_attempts."""
    import psycopg2
    for attempt in range(1, max_attempts + 1):
        try:
            conn = psycopg2.connect(database_url)
            conn.close()
            logger.info("ink.worker: Postgres reachable")
            return
        except Exception as exc:
            if attempt == max_attempts:
                logger.error(
                    "ink.worker: Postgres not reachable after %d attempts — giving up: %s",
                    max_attempts, exc,
                )
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "ink.worker: Postgres not reachable (attempt %d/%d) — retrying in %.0fs: %s",
                attempt, max_attempts, delay, exc,
            )
            time.sleep(delay)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    from config.settings import get_settings
    settings = get_settings()

    _wait_for_postgres(settings.database_url)
    with PostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        # Register InkStage so LangGraph's msgpack serializer doesn't warn about it
        try:
            checkpointer.serde.allowed_msgpack_modules.add("src.agents.ink.state")
        except AttributeError:
            pass  # older LangGraph versions don't expose this — warning is benign

        from src.agents.ink.subagents.ghost_shopper.graph import (
            build_graph as build_ghost_shopper_graph,
        )
        ghost_shopper_graph = build_ghost_shopper_graph(checkpointer=checkpointer)
        graph = build_graph(checkpointer=checkpointer, ghost_shopper_graph=ghost_shopper_graph)

        worker = InkWorker(graph)
        worker.run_forever()


if __name__ == "__main__":
    main()
