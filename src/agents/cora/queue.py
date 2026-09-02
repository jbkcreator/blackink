"""Cora draft-event queue — Redis Streams.

Adapted from Forced Action's src/agents/cora/queue.py (Redis Streams +
consumer group), but scoped to BlackInk's draft-generation context rather
than FA's LangGraph opportunity events.

Stream:         cora:drafts
Consumer group: cora_draft_workers
DLQ stream:     cora:drafts:dlq

Events that flow through this stream are draft-generation requests:
  event_type="draft.requested" → worker generates an outreach email draft
                                  and posts it to Slack for approval.

get_redis_client() returns decode_responses=True so all fields are str.

The block_ms constraint from FA is preserved: keep it comfortably under the
shared Redis client's socket_timeout (currently 30s via requests library
default — see src/core/redis_client.py). 1000ms is safe headroom.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.agents.relay.halt_service import is_halted
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

STREAM_KEY = "cora:drafts"
GROUP_NAME = "cora_draft_workers"
DLQ_KEY = "cora:drafts:dlq"
MAX_DELIVERIES = 3
# STREAM_MAXLEN is intentionally NOT used in publish(). Trimming a work queue
# at publish time evicts entries the consumer group has not yet read, silently
# losing draft requests during outages or halts. Any trim must only remove
# entries that have been acknowledged or safely archived.
STREAM_MAXLEN = 10_000


@dataclass
class DraftMessage:
    message_id: str
    event_type: str
    client_id: str
    idempotency_key: str
    payload: Dict[str, Any]
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
    event_type: str,
    client_id: str,
    payload: Dict[str, Any],
    idempotency_key: Optional[str] = None,
) -> Optional[str]:
    """XADD one draft event. Returns the Redis message id, or None on error."""
    ensure_group()
    fields = {
        "event_type": event_type,
        "client_id": client_id,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "payload": json.dumps(payload, default=str),
    }
    try:
        # No maxlen trim here — see STREAM_MAXLEN comment above.
        return get_redis_client().xadd(STREAM_KEY, fields)
    except Exception as exc:
        logger.error(
            "cora.queue: publish failed event_type=%s client_id=%s: %s",
            event_type, client_id, exc,
        )
        return None


def _parse(message_id: str, fields: Dict[str, str], delivery_count: int = 1) -> DraftMessage:
    return DraftMessage(
        message_id=message_id,
        event_type=fields.get("event_type", ""),
        client_id=fields.get("client_id", ""),
        idempotency_key=fields.get("idempotency_key", ""),
        payload=json.loads(fields.get("payload", "{}")),
        delivery_count=delivery_count,
    )


def read_batch(
    consumer_name: str,
    count: int = 1,
    block_ms: int = 1000,
) -> List[DraftMessage]:
    """XREADGROUP for new messages. block_ms must stay well under socket timeout."""
    ensure_group()
    try:
        result = get_redis_client().xreadgroup(
            GROUP_NAME, consumer_name, {STREAM_KEY: ">"}, count=count, block=block_ms
        )
    except Exception as exc:
        logger.warning("cora.queue: read_batch failed: %s", exc)
        return []
    if not result:
        return []
    messages: List[DraftMessage] = []
    for _stream, entries in result:
        for message_id, fields in entries:
            messages.append(_parse(message_id, fields))
    return messages


def ack(message_id: str) -> None:
    """Call only after the draft has been durably posted to Slack for review."""
    r = get_redis_client()
    try:
        r.xack(STREAM_KEY, GROUP_NAME, message_id)
        r.xdel(STREAM_KEY, message_id)
    except Exception as exc:
        logger.warning("cora.queue: ack failed message_id=%s: %s", message_id, exc)


def _get_stream_client_id(r, message_id: str) -> Optional[str]:
    """Peek at a stream entry to read its client_id without claiming it."""
    try:
        entries = r.xrange(STREAM_KEY, min=message_id, max=message_id)
        if entries:
            _, fields = entries[0]
            return fields.get("client_id")
    except Exception as exc:
        logger.warning(
            "cora.queue: client_id lookup failed message_id=%s: %s", message_id, exc
        )
    return None


def claim_stale(consumer_name: str, min_idle_ms: int = 60_000) -> List[DraftMessage]:
    """Reclaim pending messages idle longer than min_idle_ms; dead-letter at MAX_DELIVERIES.

    Messages whose client currently has an active halt are skipped entirely —
    they are intentionally deferred, not failed, and must not have their
    times_delivered count incremented or be moved to the DLQ while halted.
    """
    r = get_redis_client()
    try:
        pending = r.xpending_range(STREAM_KEY, GROUP_NAME, min="-", max="+", count=100)
    except Exception as exc:
        logger.warning("cora.queue: xpending_range failed: %s", exc)
        return []

    reclaimed: List[DraftMessage] = []
    to_claim: List[str] = []

    for entry in pending:
        message_id = entry["message_id"]
        delivery_count = entry.get("times_delivered", 1)
        if entry.get("time_since_delivered", 0) < min_idle_ms:
            continue

        # Do not reclaim or dead-letter messages whose client is currently
        # halted. The message is intentionally deferred — reclaiming it would
        # bump times_delivered and eventually dead-letter a valid request that
        # the operator expects to recover automatically when the halt is lifted.
        entry_client_id = _get_stream_client_id(r, message_id)
        if entry_client_id and is_halted(client_id=entry_client_id):
            logger.info(
                "cora.queue: claim_stale skipping message_id=%s "
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
        logger.warning("cora.queue: xclaim failed: %s", exc)
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
        logger.warning("cora.queue: dead-lettered message_id=%s", message_id)
    r.xack(STREAM_KEY, GROUP_NAME, message_id)
    r.xdel(STREAM_KEY, message_id)


def pending_count() -> int:
    """Unacked messages in the consumer group (the meaningful queue depth metric)."""
    try:
        summary = get_redis_client().xpending(STREAM_KEY, GROUP_NAME)
        return summary.get("pending", 0) if summary else 0
    except Exception:
        return 0


def queue_depth() -> int:
    """Total entries in the stream (includes acked, not-yet-trimmed entries)."""
    try:
        return get_redis_client().xlen(STREAM_KEY)
    except Exception:
        return 0
