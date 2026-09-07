"""Respond inbound-message queue — Redis Streams.

Mirrors the shape of src/agents/cora/queue.py (Redis Streams + consumer
group + DLQ + stale-claim sweep) but carries inbound_message DB row
references rather than draft-generation events.

Stream:         respond:inbound
Consumer group: respond_triage_workers
DLQ stream:     respond:inbound:dlq

Each message carries only enough to look up the inbound_messages DB row —
the classifier worker fetches body_text/subject/sender_email from Postgres
rather than duplicating large text payloads in Redis.

get_redis_client() returns decode_responses=True so all fields are str.
block_ms is kept at 1000ms — well under the Redis client socket timeout.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

STREAM_KEY = "respond:inbound"
GROUP_NAME = "respond_triage_workers"
DLQ_KEY = "respond:inbound:dlq"
MAX_DELIVERIES = 3


@dataclass
class InboundQueueMessage:
    message_id: str          # Redis stream entry ID
    db_id: int               # inbound_messages.id (PK)
    client_id: str
    idempotency_key: str
    delivery_count: int = 1


def ensure_group() -> None:
    """Idempotent — creates the stream + consumer group if either is missing."""
    r = get_redis_client()
    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def publish(
    db_id: int,
    client_id: str,
    idempotency_key: str,
) -> Optional[str]:
    """XADD one inbound-message event. Returns the Redis message id or None."""
    ensure_group()
    fields = {
        "db_id": str(db_id),
        "client_id": client_id,
        "idempotency_key": idempotency_key,
    }
    try:
        return get_redis_client().xadd(STREAM_KEY, fields)
    except Exception as exc:
        logger.error(
            "respond.queue: publish failed db_id=%s client_id=%s: %s",
            db_id, client_id, exc,
        )
        return None


def _parse(message_id: str, fields: Dict[str, str], delivery_count: int = 1) -> InboundQueueMessage:
    return InboundQueueMessage(
        message_id=message_id,
        db_id=int(fields.get("db_id", 0)),
        client_id=fields.get("client_id", ""),
        idempotency_key=fields.get("idempotency_key", ""),
        delivery_count=delivery_count,
    )


def read_batch(
    consumer_name: str,
    count: int = 1,
    block_ms: int = 1000,
) -> List[InboundQueueMessage]:
    """XREADGROUP — claim new messages from the stream."""
    ensure_group()
    try:
        result = get_redis_client().xreadgroup(
            GROUP_NAME, consumer_name, {STREAM_KEY: ">"}, count=count, block=block_ms
        )
    except Exception as exc:
        logger.warning("respond.queue: read_batch failed: %s", exc)
        return []
    if not result:
        return []
    messages: List[InboundQueueMessage] = []
    for _stream, entries in result:
        for message_id, fields in entries:
            messages.append(_parse(message_id, fields))
    return messages


def ack(message_id: str) -> None:
    """Acknowledge after the DB classification write has committed."""
    try:
        get_redis_client().xack(STREAM_KEY, GROUP_NAME, message_id)
    except Exception as exc:
        logger.warning("respond.queue: ack failed message_id=%s: %s", message_id, exc)


def claim_stale(consumer_name: str, min_idle_ms: int = 60_000) -> List[InboundQueueMessage]:
    """Reclaim pending messages idle longer than min_idle_ms; dead-letter at MAX_DELIVERIES."""
    r = get_redis_client()
    try:
        pending = r.xpending_range(STREAM_KEY, GROUP_NAME, min="-", max="+", count=100)
    except Exception as exc:
        logger.warning("respond.queue: xpending_range failed: %s", exc)
        return []

    reclaimed: List[InboundQueueMessage] = []
    to_claim: List[str] = []

    for entry in pending:
        message_id = entry["message_id"]
        delivery_count = entry.get("times_delivered", 1)
        if entry.get("time_since_delivered", 0) < min_idle_ms:
            continue
        if delivery_count >= MAX_DELIVERIES:
            _dead_letter(r, message_id)
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
        logger.warning("respond.queue: xclaim failed: %s", exc)
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


def _dead_letter(r: Any, message_id: str) -> None:
    entries = r.xrange(STREAM_KEY, min=message_id, max=message_id)
    if entries:
        _, fields = entries[0]
        r.xadd(DLQ_KEY, {**fields, "original_message_id": message_id, "dlq_reason": "max_deliveries_exceeded"})
        logger.warning("respond.queue: dead-lettered message_id=%s", message_id)
    r.xack(STREAM_KEY, GROUP_NAME, message_id)


def pending_count() -> int:
    try:
        summary = get_redis_client().xpending(STREAM_KEY, GROUP_NAME)
        return summary.get("pending", 0) if summary else 0
    except Exception:
        return 0
