"""relay:sends Redis Stream — approved campaign dispatches.

Published by node_relay_dispatch (src/agents/ink/nodes.py) when a Slack
approval clears wait_approve. Consumed by src/agents/relay/worker.py.

Pattern mirrors ink/queue.py — halt-aware claim_stale(), xdel on ack,
DLQ at MAX_DELIVERIES.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from src.agents.relay.halt_service import is_halted
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

STREAM_KEY     = "relay:sends"
GROUP_NAME     = "relay_send_workers"
DLQ_KEY        = "relay:sends:dlq"
MAX_DELIVERIES = 3


@dataclass
class SendMessage:
    message_id:       str
    work_order_id:    str
    campaign_id:      str
    company_id:       str
    client_id:        str
    draft_message_id: str
    pdf_url:          Optional[str]
    video_id:         Optional[str]
    landing_url:      Optional[str]
    gif_url:          Optional[str]
    delivery_count:   int = 1


def ensure_group() -> None:
    r = get_redis_client()
    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def publish(
    work_order_id:    str,
    campaign_id:      str,
    company_id:       str,
    client_id:        str,
    draft_message_id: str,
    pdf_url:          Optional[str] = None,
    video_id:         Optional[str] = None,
    landing_url:      Optional[str] = None,
    gif_url:          Optional[str] = None,
) -> Optional[str]:
    """Enqueue an approved send. Returns the Redis message id."""
    ensure_group()
    fields = {
        "work_order_id":    work_order_id,
        "campaign_id":      campaign_id,
        "company_id":       company_id,
        "client_id":        client_id,
        "draft_message_id": draft_message_id,
        "pdf_url":          pdf_url or "",
        "video_id":         video_id or "",
        "landing_url":      landing_url or "",
        "gif_url":          gif_url or "",
    }
    try:
        return get_redis_client().xadd(STREAM_KEY, fields)
    except Exception as exc:
        logger.error(
            "relay.sends_queue: publish failed work_order_id=%s: %s",
            work_order_id, exc,
        )
        raise


def _parse(message_id: str, fields: dict, delivery_count: int = 1) -> SendMessage:
    return SendMessage(
        message_id=message_id,
        work_order_id=fields.get("work_order_id", ""),
        campaign_id=fields.get("campaign_id", ""),
        company_id=fields.get("company_id", ""),
        client_id=fields.get("client_id", ""),
        draft_message_id=fields.get("draft_message_id", ""),
        pdf_url=fields.get("pdf_url") or None,
        video_id=fields.get("video_id") or None,
        landing_url=fields.get("landing_url") or None,
        gif_url=fields.get("gif_url") or None,
        delivery_count=delivery_count,
    )


def read_batch(
    consumer_name: str,
    count: int = 1,
    block_ms: int = 1000,
) -> List[SendMessage]:
    ensure_group()
    try:
        result = get_redis_client().xreadgroup(
            GROUP_NAME, consumer_name, {STREAM_KEY: ">"}, count=count, block=block_ms,
        )
    except Exception as exc:
        logger.warning("relay.sends_queue: read_batch failed: %s", exc)
        return []
    if not result:
        return []
    messages = []
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
        logger.warning("relay.sends_queue: ack failed message_id=%s: %s", message_id, exc)


def claim_stale(consumer_name: str, min_idle_ms: int = 60_000) -> List[SendMessage]:
    """Reclaim stale pending sends; dead-letter at MAX_DELIVERIES.

    Halted-client sends are skipped — do not reclaim while CLIENT halt is active.
    """
    r = get_redis_client()
    try:
        pending = r.xpending_range(STREAM_KEY, GROUP_NAME, min="-", max="+", count=100)
    except Exception as exc:
        logger.warning("relay.sends_queue: xpending_range failed: %s", exc)
        return []

    reclaimed: List[SendMessage] = []
    to_claim: List[str] = []

    for entry in pending:
        message_id     = entry["message_id"]
        delivery_count = entry.get("times_delivered", 1)
        if entry.get("time_since_delivered", 0) < min_idle_ms:
            continue

        try:
            entries = r.xrange(STREAM_KEY, min=message_id, max=message_id)
            if entries:
                _, fields = entries[0]
                client_id = fields.get("client_id")
                if client_id and is_halted(client_id=client_id):
                    logger.info(
                        "relay.sends_queue: skipping halted client_id=%s message_id=%s",
                        client_id, message_id,
                    )
                    continue
        except Exception:
            pass

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
        logger.warning("relay.sends_queue: xclaim failed: %s", exc)
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


def _dead_letter(r, message_id: str) -> None:
    entries = r.xrange(STREAM_KEY, min=message_id, max=message_id)
    if entries:
        _, fields = entries[0]
        r.xadd(DLQ_KEY, {**fields, "original_message_id": message_id, "dlq_reason": "max_deliveries_exceeded"})
        logger.warning("relay.sends_queue: dead-lettered message_id=%s", message_id)
    r.xack(STREAM_KEY, GROUP_NAME, message_id)
    r.xdel(STREAM_KEY, message_id)
