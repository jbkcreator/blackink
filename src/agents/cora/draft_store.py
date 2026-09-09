"""Campaign draft storage — Redis-backed, keyed by work_order_id.

Cora writes the approved 5-touch sequence here after LLM generation.
Relay worker reads it at dispatch time.

Key:   ink:drafts:{work_order_id}
Value: JSON-encoded list of step dicts:
       [{"step_number": int, "delay_days": int, "subject": str, "body": str}, ...]
TTL:   DRAFT_TTL_SEC (default 30 days) — drafts expire automatically; a
       relay worker that picks up a send message after the draft has expired
       logs DRAFT_EXPIRED and dead-letters the message.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

DRAFT_TTL_SEC = 30 * 24 * 3600  # 30 days
_KEY_PREFIX   = "ink:drafts:"


def _key(work_order_id: str) -> str:
    return f"{_KEY_PREFIX}{work_order_id}"


def put(work_order_id: str, steps: List[dict]) -> None:
    """Store draft steps. Overwrites any existing draft for this work order."""
    r = get_redis_client()
    r.set(_key(work_order_id), json.dumps(steps), ex=DRAFT_TTL_SEC)
    logger.info("draft_store: stored %d steps for work_order_id=%s", len(steps), work_order_id)


def get(work_order_id: str) -> Optional[List[dict]]:
    """Return the stored steps, or None if missing / expired."""
    r = get_redis_client()
    raw = r.get(_key(work_order_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("draft_store: corrupt draft for work_order_id=%s: %s", work_order_id, exc)
        return None


def delete(work_order_id: str) -> None:
    get_redis_client().delete(_key(work_order_id))
