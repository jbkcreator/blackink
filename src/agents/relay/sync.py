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

    try:
        r = get_redis_client()
    except Exception as exc:
        # Redis unavailable — do not block startup. is_halted() falls back to
        # Postgres on every call until a later sync succeeds. Matches this
        # module's own contract (see docstring: "preferable to blocking startup").
        logger.error(
            "relay.sync: Redis unavailable (%s) — skipping sync; "
            "is_halted() will use its Postgres fallback", exc
        )
        return 0

    synced = 0
    active_keys: set = set()

    for row in rows:
        key = _redis_key(row.scope, row.scope_id)
        active_keys.add(key)
        try:
            r.set(key, str(row.id))  # no TTL
            synced += 1
        except Exception as exc:
            logger.error(
                "relay.sync: failed to set Redis key for halt_id=%d: %s", row.id, exc
            )

    # Reconcile: delete any Redis halt keys not in the active Postgres set.
    # This covers the case where resume_halt() cleared Postgres but its Redis
    # delete failed — without this step, sync would leave the stale key in
    # place, set relay:synced, and is_halted() would trust it forever.
    try:
        for key in r.scan_iter("relay:halt:*"):
            if key not in active_keys:
                r.delete(key)
                logger.info("relay.sync: removed stale Redis halt key %s", key)
    except Exception as exc:
        logger.error("relay.sync: failed during stale-key reconciliation: %s", exc)

    try:
        r.set(_REDIS_SYNCED_KEY, "1")
    except Exception as exc:
        logger.error("relay.sync: failed to set relay:synced flag: %s", exc)

    logger.info("relay.sync: synced %d active halt(s) from Postgres to Redis", synced)
    return synced
