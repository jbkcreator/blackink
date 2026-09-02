"""Relay halt service — dual-persistence (Postgres + Redis), no TTL.

Two layers, one source of truth:
  Postgres (relay_halts) — the durable record, survives Redis restarts.
  Redis (relay:halt:*) — the fast-path check, synced from Postgres at startup
                         by sync.sync_halts_from_db().

Redis keys carry NO TTL. A halt persists until an authorized admin calls
resume_halt() with a valid HMAC token. There is no auto-resume, no expiry,
no time-based re-arm. The lock holds across server restarts.

is_halted() check order:
  1. Redis exists(key) — O(1), fast path for the hot worker loop.
  2. If key absent AND relay:synced flag absent → Postgres fallback (pre-sync
     window on a fresh Redis / restart race). Once sync completes and sets
     relay:synced, step 2 is skipped and a missing key definitively means
     no halt — no per-request DB round-trip.
  3. Postgres fallback: fail-open (False) if both layers are unavailable, to
     avoid deadlocking all workers when infra is degraded. The underlying halt
     record is still durable in Postgres; sync will restore it on recovery.

Cascade: is_halted(client_id="acme") checks GLOBAL and CLIENT:acme. Passing
campaign_id also checks CAMPAIGN:campaign_id. Any one matching scope → halted.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from src.agents.relay.halt_state import VALID_SCOPES, HaltRecord
from src.agents.relay.resume_auth import verify_resume_token
from src.core.database import get_system_db_context
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_REDIS_SYNCED_KEY = "relay:synced"


# ── Redis key helpers ─────────────────────────────────────────────────────────

def _redis_key(scope: str, scope_id: Optional[str] = None) -> str:
    if scope == "GLOBAL":
        return "relay:halt:global"
    if scope == "CLIENT":
        return f"relay:halt:client:{scope_id}"
    if scope == "CAMPAIGN":
        return f"relay:halt:campaign:{scope_id}"
    raise ValueError(f"Unknown halt scope: {scope!r}")


def _set_redis_halt(scope: str, scope_id: Optional[str], halt_id: int) -> None:
    try:
        get_redis_client().set(_redis_key(scope, scope_id), str(halt_id))  # no EX — permanent
    except Exception as exc:
        logger.error(
            "relay: failed to set Redis halt key scope=%s scope_id=%s halt_id=%d: %s",
            scope, scope_id, halt_id, exc,
        )


def _clear_redis_halt(scope: str, scope_id: Optional[str]) -> None:
    try:
        get_redis_client().delete(_redis_key(scope, scope_id))
    except Exception as exc:
        logger.error(
            "relay: failed to clear Redis halt key scope=%s scope_id=%s: %s",
            scope, scope_id, exc,
        )


# ── Core API ──────────────────────────────────────────────────────────────────

def issue_halt(
    scope: str,
    *,
    scope_id: Optional[str] = None,
    reason: str,
    issued_by: str,
) -> int:
    """Persist a halt to Postgres then Redis. Returns the halt_id.

    Idempotent per (scope, scope_id): if an active halt already exists for
    this scope/scope_id, returns its existing id without creating a duplicate.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope {scope!r}. Must be one of {VALID_SCOPES}")
    if scope != "GLOBAL" and not scope_id:
        raise ValueError(f"scope_id is required when scope={scope!r}")

    now = datetime.now(timezone.utc)

    with get_system_db_context() as session:
        row = session.execute(
            text("""
                WITH existing AS (
                    SELECT id FROM relay_halts
                    WHERE scope = :scope
                      AND COALESCE(scope_id, '') = COALESCE(:scope_id, '')
                      AND is_active IS TRUE
                ),
                inserted AS (
                    INSERT INTO relay_halts (scope, scope_id, reason, issued_by, issued_at, is_active)
                    SELECT :scope, :scope_id, :reason, :issued_by, :issued_at, TRUE
                    WHERE NOT EXISTS (SELECT 1 FROM existing)
                    RETURNING id
                )
                SELECT id FROM inserted
                UNION ALL
                SELECT id FROM existing
                LIMIT 1
            """),
            {
                "scope": scope,
                "scope_id": scope_id,
                "reason": reason,
                "issued_by": issued_by,
                "issued_at": now,
            },
        ).first()

    halt_id = row[0]
    _set_redis_halt(scope, scope_id, halt_id)

    logger.warning(
        "relay: halt issued — halt_id=%d scope=%s scope_id=%s reason=%r issued_by=%s",
        halt_id, scope, scope_id, reason, issued_by,
    )
    return halt_id


def is_halted(
    client_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
) -> bool:
    """True if any applicable halt is active.

    Checks cascade: GLOBAL is always checked; CLIENT:{client_id} if provided;
    CAMPAIGN:{campaign_id} if provided. Any one active scope → True.
    """
    checks = [("GLOBAL", None)]
    if client_id:
        checks.append(("CLIENT", client_id))
    if campaign_id:
        checks.append(("CAMPAIGN", campaign_id))

    try:
        r = get_redis_client()
        for scope, sid in checks:
            if r.exists(_redis_key(scope, sid)):
                return True
        # If synced flag is present, Redis picture is authoritative.
        if r.exists(_REDIS_SYNCED_KEY):
            return False
    except Exception as exc:
        logger.error("relay: Redis halt check failed, falling back to Postgres: %s", exc)

    # Pre-sync window or Redis unavailable — fall back to Postgres.
    return _db_is_halted(client_id=client_id, campaign_id=campaign_id)


def _db_is_halted(client_id: Optional[str], campaign_id: Optional[str]) -> bool:
    try:
        with get_system_db_context() as session:
            row = session.execute(
                text("""
                    SELECT 1 FROM relay_halts
                    WHERE is_active IS TRUE
                      AND (
                          scope = 'GLOBAL'
                          OR (scope = 'CLIENT'   AND scope_id = :client_id   AND :client_id   IS NOT NULL)
                          OR (scope = 'CAMPAIGN' AND scope_id = :campaign_id AND :campaign_id IS NOT NULL)
                      )
                    LIMIT 1
                """),
                {"client_id": client_id, "campaign_id": campaign_id},
            ).first()
            return row is not None
    except Exception as exc:
        logger.error(
            "relay: Postgres halt check failed: %s — treating as NOT halted (fail-open)",
            exc,
        )
        return False


def resume_halt(halt_id: int, *, token: str, resumed_by: str) -> bool:
    """Verify HMAC token, mark halt inactive in Postgres, clear Redis key.

    Returns True on success, False if token is invalid or halt not found.
    Never clears a halt without a valid token.
    """
    if not verify_resume_token(halt_id, token):
        logger.warning(
            "relay: resume rejected — invalid token for halt_id=%d requested_by=%s",
            halt_id, resumed_by,
        )
        return False

    with get_system_db_context() as session:
        row = session.execute(
            text("""
                UPDATE relay_halts
                   SET is_active  = FALSE,
                       resumed_at = :now,
                       resumed_by = :resumed_by
                 WHERE id = :halt_id
                   AND is_active IS TRUE
                RETURNING scope, scope_id
            """),
            {
                "halt_id": halt_id,
                "now": datetime.now(timezone.utc),
                "resumed_by": resumed_by,
            },
        ).first()

    if row is None:
        logger.warning(
            "relay: resume_halt — halt_id=%d not found or already inactive", halt_id
        )
        return False

    scope, scope_id = row.scope, row.scope_id
    _clear_redis_halt(scope, scope_id)

    logger.warning(
        "relay: halt resumed — halt_id=%d scope=%s scope_id=%s resumed_by=%s",
        halt_id, scope, scope_id, resumed_by,
    )
    return True


def get_active_halts() -> list:
    """Return all active HaltRecord instances, newest first.

    Used by the Slack cockpit (Dev 3) to list active halts in
    #blackink-command. Returns an empty list if Postgres is unavailable.
    """
    try:
        with get_system_db_context() as session:
            rows = session.execute(
                text("""
                    SELECT id, scope, scope_id, reason, issued_by, issued_at
                      FROM relay_halts
                     WHERE is_active IS TRUE
                     ORDER BY issued_at DESC
                """)
            ).fetchall()
    except Exception as exc:
        logger.error("relay: get_active_halts failed: %s", exc)
        return []

    return [
        HaltRecord(
            halt_id=r.id,
            scope=r.scope,
            scope_id=r.scope_id,
            reason=r.reason,
            issued_by=r.issued_by,
            issued_at=r.issued_at,
        )
        for r in rows
    ]
