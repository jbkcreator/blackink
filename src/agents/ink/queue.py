"""Ink (Campaign Agent) work-order queue — Redis Streams.

Stream:         ink:work_orders
Consumer group: ink_workers
DLQ stream:     ink:work_orders:dlq

Work orders are published here when a company is cleared by the compliance
gate and ready for a full campaign run. The Ink worker picks them up,
creates the initial GlobalState, and invokes the LangGraph campaign graph.

Pattern mirrors src/agents/cora/queue.py exactly — halt-aware claim_stale(),
xdel on ack, DLQ on max deliveries.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import List, Optional

from src.agents.relay.halt_service import is_halted
from src.agents.ink.state import WorkOrderMessage
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

STREAM_KEY  = "ink:work_orders"
GROUP_NAME  = "ink_workers"
DLQ_KEY     = "ink:work_orders:dlq"
MAX_DELIVERIES = 3


def ensure_group() -> None:
    r = get_redis_client()
    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def publish(
    company_id:     str,
    campaign_id:    str,
    work_order_id:  str,
    client_id:      str,
) -> Optional[str]:
    """Enqueue a new campaign work order. Returns the Redis message id."""
    ensure_group()
    fields = {
        "company_id":    company_id,
        "campaign_id":   campaign_id,
        "work_order_id": work_order_id,
        "client_id":     client_id,
    }
    try:
        return get_redis_client().xadd(STREAM_KEY, fields)
    except Exception as exc:
        logger.error(
            "ink.queue: publish failed company_id=%s work_order_id=%s: %s",
            company_id, work_order_id, exc,
        )
        raise


def _parse(message_id: str, fields: dict, delivery_count: int = 1) -> WorkOrderMessage:
    return WorkOrderMessage(
        message_id=message_id,
        company_id=fields.get("company_id", ""),
        campaign_id=fields.get("campaign_id", ""),
        work_order_id=fields.get("work_order_id", ""),
        client_id=fields.get("client_id", ""),
        delivery_count=delivery_count,
    )


def read_batch(
    consumer_name: str,
    count: int = 1,
    block_ms: int = 1000,
) -> List[WorkOrderMessage]:
    ensure_group()
    try:
        result = get_redis_client().xreadgroup(
            GROUP_NAME, consumer_name, {STREAM_KEY: ">"}, count=count, block=block_ms
        )
    except Exception as exc:
        logger.warning("ink.queue: read_batch failed: %s", exc)
        return []
    if not result:
        return []
    messages: List[WorkOrderMessage] = []
    for _stream, entries in result:
        for message_id, fields in entries:
            messages.append(_parse(message_id, fields))
    return messages


def ack(message_id: str) -> None:
    r = get_redis_client()
    try:
        r.xack(STREAM_KEY, GROUP_NAME, message_id)
        r.xdel(STREAM_KEY, message_id)
    except Exception as exc:
        logger.warning("ink.queue: ack failed message_id=%s: %s", message_id, exc)


def _get_stream_client_id(r, message_id: str) -> Optional[str]:
    try:
        entries = r.xrange(STREAM_KEY, min=message_id, max=message_id)
        if entries:
            _, fields = entries[0]
            return fields.get("client_id")
    except Exception as exc:
        logger.warning(
            "ink.queue: client_id lookup failed message_id=%s: %s", message_id, exc
        )
    return None


def claim_stale(consumer_name: str, min_idle_ms: int = 60_000) -> List[WorkOrderMessage]:
    """Reclaim stale pending work orders; dead-letter at MAX_DELIVERIES.

    Halted-client work orders are skipped — same semantics as cora:drafts.
    """
    r = get_redis_client()
    try:
        pending = r.xpending_range(STREAM_KEY, GROUP_NAME, min="-", max="+", count=100)
    except Exception as exc:
        logger.warning("ink.queue: xpending_range failed: %s", exc)
        return []

    reclaimed: List[WorkOrderMessage] = []
    to_claim: List[str] = []

    for entry in pending:
        message_id     = entry["message_id"]
        delivery_count = entry.get("times_delivered", 1)
        if entry.get("time_since_delivered", 0) < min_idle_ms:
            continue

        entry_client_id = _get_stream_client_id(r, message_id)
        if entry_client_id and is_halted(client_id=entry_client_id):
            logger.info(
                "ink.queue: claim_stale skipping message_id=%s "
                "— CLIENT halt active for client_id=%s",
                message_id, entry_client_id,
            )
            continue

        if delivery_count >= MAX_DELIVERIES:
            _dead_letter_pending(r, message_id)
            continue
        to_claim.append(message_id)

    if not to_claim:
        return reclaimed

    try:
        claimed = r.xclaim(
            STREAM_KEY, GROUP_NAME, consumer_name,
            min_idle_time=min_idle_ms, message_ids=to_claim,
        )
    except Exception as exc:
        logger.warning("ink.queue: xclaim failed: %s", exc)
        return reclaimed

    for message_id, fields in claimed:
        if fields is None:
            continue
        dc = next(
            (e.get("times_delivered", 1) for e in pending if e["message_id"] == message_id),
            1,
        )
        reclaimed.append(_parse(message_id, fields, delivery_count=dc + 1))

    return reclaimed


def _dead_letter_pending(r, message_id: str) -> None:
    entries = r.xrange(STREAM_KEY, min=message_id, max=message_id)
    if entries:
        _, fields = entries[0]
        r.xadd(DLQ_KEY, {**fields, "original_message_id": message_id, "dlq_reason": "max_deliveries_exceeded"})
        logger.warning("ink.queue: dead-lettered message_id=%s", message_id)
    r.xack(STREAM_KEY, GROUP_NAME, message_id)
    r.xdel(STREAM_KEY, message_id)


def pending_count() -> int:
    try:
        summary = get_redis_client().xpending(STREAM_KEY, GROUP_NAME)
        return summary.get("pending", 0) if summary else 0
    except Exception:
        return 0


def queue_depth() -> int:
    try:
        return get_redis_client().xlen(STREAM_KEY)
    except Exception:
        return 0
