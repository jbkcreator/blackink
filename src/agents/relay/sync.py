"""Relay startup sync — restore Redis halt state from Postgres.

Called once at app/worker startup (before the worker loop begins) so Redis
reflects the durable Postgres state even after a Redis restart. Without this,
a Redis flush would silently clear all active halts until an admin noticed and
re-issued them.

After sync completes, relay:synced is set in Redis. is_halted() in
halt_service.py uses this flag to know the Redis picture is authoritative and
skip the per-request Postgres fallback on every call.

If Postgres is unreachable at startup, sync logs an error and returns 0 — the
worker may begin running but is_halted()'s Postgres fallback will fire on
every call until sync can succeed on a retry. This is preferable to blocking
startup indefinitely on a DB outage.
"""

import logging

from sqlalchemy import text

from src.agents.relay.halt_service import _redis_key, _REDIS_SYNCED_KEY
from src.core.database import get_system_db_context
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)


def sync_halts_from_db() -> int:
    """Read active halts from Postgres, write them to Redis (no TTL).

    Returns the number of halt keys successfully synced.
    """
    try:
        with get_system_db_context() as session:
            rows = session.execute(
                text(
                    "SELECT id, scope, scope_id FROM relay_halts WHERE is_active IS TRUE"
                )
            ).fetchall()
    except Exception as exc:
        logger.error(
            "relay.sync: failed to read active halts from Postgres: %s — skipping sync", exc
        )
        return 0

    r = get_redis_client()
    synced = 0
    for row in rows:
        try:
            r.set(_redis_key(row.scope, row.scope_id), str(row.id))  # no TTL
            synced += 1
        except Exception as exc:
            logger.error(
                "relay.sync: failed to set Redis key for halt_id=%d: %s", row.id, exc
            )

    try:
        r.set(_REDIS_SYNCED_KEY, "1")
    except Exception as exc:
        logger.error("relay.sync: failed to set relay:synced flag: %s", exc)

    logger.info("relay.sync: synced %d active halt(s) from Postgres to Redis", synced)
    return synced
