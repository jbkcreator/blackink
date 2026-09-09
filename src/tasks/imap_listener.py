"""Ghost Shopper IMAP listener — monitors audit-bot inbox for PM firm replies.

When Ghost Shopper submits an inquiry form on a PM firm's website, the node
writes ghost_submitted_at + ghost_work_order_id to contacts and the Ink graph
suspends at WAIT_REPLY. This listener:

  1. Connects to the audit-bot IMAP mailbox via IMAP IDLE (real-time push).
  2. On each new message, matches the sender's domain against contacts that
     have a pending ghost_submitted_at, computes latency_sec + loss_est, and
     publishes a resume signal to ink:resume_signals so the Ink worker resumes
     the LangGraph from WAIT_REPLY.
  3. Runs a periodic timeout sweep: submissions older than
     GHOST_REPLY_TIMEOUT_HOURS get a null resume signal so the campaign
     continues without audit data rather than staying suspended forever.

Fail-closed on IMAP_ENABLED=False: exits immediately (campaigns stay
suspended rather than silently skipping latency data).

Usage
─────
    python -m src.tasks.imap_listener

Environment
───────────
    IMAP_ENABLED          must be True to run (default False)
    IMAP_HOST             IMAP server hostname (default imap.gmail.com)
    IMAP_PORT             IMAP SSL port (default 993)
    IMAP_USER             mailbox address (default audit-bot@audit-blackink.com)
    IMAP_PASSWORD         app password (required when IMAP_ENABLED=True)
    GHOST_REPLY_TIMEOUT_HOURS   hours before a null resume fires (default 24)
    DATABASE_URL_SYSTEM   BYPASSRLS DSN (contacts are RLS-scoped via companies)
    REDIS_URL             ink:resume_signals stream target
"""
from __future__ import annotations

import asyncio
import email
import json
import logging
import math
import sys
import time
from email.utils import parseaddr

import aioimaplib
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.core.redis_client import get_redis_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Revenue loss formula constants ───────────────────────────────────────────
# Est. Annual Lost Revenue =
#   MONTHLY_LEADS × (1 − e^(−0.0005 × latency_sec)) × AVG_FEE_ANNUAL × AVG_TENURE_YEARS
# Defaults from Subtask 2.1.3 spec: $100/door/month fee base, 30-month tenure.
_MONTHLY_LEADS      = 8      # assumed new owner inquiries per month for a typical PM firm
_AVG_FEE_ANNUAL     = 1_200  # $100/door/month × 12 months
_AVG_TENURE_YEARS   = 2.5    # 30-month average owner tenure

# ── IMAP tuning ───────────────────────────────────────────────────────────────
_IDLE_REFRESH_SEC        = 25 * 60   # re-enter IDLE every 25 min (server times out ~29)
_TIMEOUT_SWEEP_INTERVAL  = 5 * 60    # check for stale submissions every 5 minutes
_RECONNECT_DELAY_SEC     = 30        # wait before reconnecting after an error
_FETCH_BATCH             = 50        # max UNSEEN messages to fetch per pass

RESUME_STREAM_KEY = "ink:resume_signals"


# ── Revenue formula ───────────────────────────────────────────────────────────

def _compute_loss_est(latency_sec: int) -> int:
    decay = 1.0 - math.exp(-0.0005 * latency_sec)
    return int(_MONTHLY_LEADS * decay * _AVG_FEE_ANNUAL * _AVG_TENURE_YEARS)


# ── Domain extraction ─────────────────────────────────────────────────────────

def _parse_sender_domain(from_header: str) -> str | None:
    """Extract the apex domain from a From: header.

    'Jordan Mitchell <inquiry@suncoastpm.com>' → 'suncoastpm.com'
    Returns None for malformed or empty headers.
    """
    _, addr = parseaddr(from_header)
    if not addr or "@" not in addr:
        return None
    domain = addr.split("@", 1)[1].lower().strip()
    # Strip www. prefix — companies.domain is apex only.
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _find_pending_submission(db, sender_domain: str):
    """Return (ghost_submitted_at, ghost_work_order_id, company_id) for the
    first contact whose company domain matches and has a pending ghost submission.

    Uses DISTINCT ON (co.company_id) so that two contacts per company don't
    produce two rows — both carry the same ghost_submitted_at / ghost_work_order_id.
    Runs under get_system_db_context (BYPASSRLS) because contacts are RLS-scoped
    via companies.owning_client_id = NULL for unallocated prospects.
    """
    return db.execute(
        text("""
            SELECT DISTINCT ON (co.company_id)
                c.ghost_submitted_at,
                c.ghost_work_order_id,
                co.company_id
            FROM contacts c
            JOIN companies co ON co.company_id = c.company_id
            WHERE co.domain = :domain
              AND c.ghost_submitted_at  IS NOT NULL
              AND c.ghost_work_order_id IS NOT NULL
            LIMIT 1
        """),
        {"domain": sender_domain},
    ).fetchone()


def _clear_pending(db, company_id: str) -> None:
    """Null out the ghost marker columns once a reply (or timeout) is processed."""
    db.execute(
        text(
            "UPDATE contacts "
            "SET ghost_submitted_at  = NULL, "
            "    ghost_work_order_id = NULL "
            "WHERE company_id = :cid"
        ),
        {"cid": company_id},
    )


def _record_reply(
    db,
    work_order_id: str,
    company_id:    str,
    sender_domain: str | None,
    received_at_ms: int,
    latency_sec:   int | None,
    loss_est:      int | None,
    timed_out:     bool = False,
) -> None:
    """Insert one row into ghost_shopper_replies (audit log).

    Called inside the same DB transaction as _clear_pending so both commit
    or roll back together. Insert happens before the Redis publish so a
    Redis failure leaves the DB clean (transaction not yet committed).
    """
    db.execute(
        text("""
            INSERT INTO ghost_shopper_replies
                (work_order_id, company_id, sender_domain, received_at,
                 latency_sec, loss_est, timed_out)
            VALUES
                (:work_order_id, :company_id, :sender_domain,
                 to_timestamp(:received_at_ms / 1000.0),
                 :latency_sec, :loss_est, :timed_out)
        """),
        {
            "work_order_id":  work_order_id,
            "company_id":     company_id,
            "sender_domain":  sender_domain,
            "received_at_ms": received_at_ms,
            "latency_sec":    latency_sec,
            "loss_est":       loss_est,
            "timed_out":      timed_out,
        },
    )


def _find_timed_out_submissions(db, cutoff_ms: int) -> list:
    """Return all pending ghost submissions older than cutoff_ms (epoch ms)."""
    return db.execute(
        text("""
            SELECT DISTINCT ON (co.company_id)
                c.ghost_submitted_at,
                c.ghost_work_order_id,
                co.company_id
            FROM contacts c
            JOIN companies co ON co.company_id = c.company_id
            WHERE c.ghost_submitted_at  IS NOT NULL
              AND c.ghost_work_order_id IS NOT NULL
              AND c.ghost_submitted_at < :cutoff
        """),
        {"cutoff": cutoff_ms},
    ).fetchall()


# ── Resume signal publishing ──────────────────────────────────────────────────

def _publish_resume(work_order_id: str, latency_sec: int | None, loss_est: int | None) -> None:
    """Write one entry to ink:resume_signals for the Ink worker to pick up."""
    payload = json.dumps({"latency_sec": latency_sec, "loss_est": loss_est})
    get_redis_client().xadd(
        RESUME_STREAM_KEY,
        {"work_order_id": work_order_id, "resume_payload": payload},
    )
    logger.info(
        "imap_listener: published resume signal work_order_id=%s latency_sec=%s loss_est=%s",
        work_order_id, latency_sec, loss_est,
    )


# ── Core reply handler ────────────────────────────────────────────────────────

def _handle_reply(sender_domain: str, received_at_ms: int) -> None:
    """Process one inbound reply: look up the pending submission, compute
    latency + loss, publish a resume signal, and clear the DB markers."""
    with get_system_db_context() as db:
        row = _find_pending_submission(db, sender_domain)
        if not row:
            logger.info(
                "imap_listener: no pending ghost submission for domain=%s — ignoring",
                sender_domain,
            )
            return

        latency_sec = max(0, int((received_at_ms - row.ghost_submitted_at) / 1000))
        loss_est    = _compute_loss_est(latency_sec)

        # Audit row first — inside the transaction, before Redis publish.
        # If Redis fails, the transaction rolls back cleanly.
        _record_reply(
            db,
            work_order_id=row.ghost_work_order_id,
            company_id=row.company_id,
            sender_domain=sender_domain,
            received_at_ms=received_at_ms,
            latency_sec=latency_sec,
            loss_est=loss_est,
            timed_out=False,
        )
        _publish_resume(row.ghost_work_order_id, latency_sec, loss_est)
        _clear_pending(db, row.company_id)
        db.commit()

    logger.info(
        "imap_listener: reply processed company_id=%s domain=%s latency_sec=%d loss_est=%d",
        row.company_id, sender_domain, latency_sec, loss_est,
    )


# ── Timeout sweep ─────────────────────────────────────────────────────────────

def _sweep_timeouts(timeout_hours: int) -> None:
    """Publish null resume signals for submissions that have waited too long.

    A null resume lets the campaign continue without latency data rather than
    staying suspended at WAIT_REPLY forever.
    """
    cutoff_ms = int((time.time() - timeout_hours * 3600) * 1000)
    now_ms = int(time.time() * 1000)
    with get_system_db_context() as db:
        rows = _find_timed_out_submissions(db, cutoff_ms)
        for row in rows:
            _record_reply(
                db,
                work_order_id=row.ghost_work_order_id,
                company_id=row.company_id,
                sender_domain=None,
                received_at_ms=now_ms,
                latency_sec=None,
                loss_est=None,
                timed_out=True,
            )
            _publish_resume(row.ghost_work_order_id, latency_sec=None, loss_est=None)
            _clear_pending(db, row.company_id)
            logger.info(
                "imap_listener: timeout sweep — timed out company_id=%s submitted_at=%d",
                row.company_id, row.ghost_submitted_at,
            )
        if rows:
            db.commit()


# ── IMAP fetch helpers ────────────────────────────────────────────────────────

async def _fetch_unseen(client: aioimaplib.IMAP4_SSL) -> list[dict]:
    """Return From + Date headers for all UNSEEN messages (up to _FETCH_BATCH)."""
    _, data = await client.search("UNSEEN")
    raw = data[0].decode() if data and data[0] else ""
    uid_list = raw.split()
    if not uid_list:
        return []

    # Process most recent batch first.
    uids = uid_list[-_FETCH_BATCH:]
    results = []
    for uid in uids:
        _, msg_data = await client.fetch(uid, "(BODY[HEADER.FIELDS (FROM DATE)])")
        if not msg_data:
            continue
        # aioimaplib returns a list; the header bytes are typically at index 1.
        raw_bytes = None
        for part in msg_data:
            if isinstance(part, bytes) and part:
                raw_bytes = part
                break
        if raw_bytes is None:
            continue
        msg = email.message_from_bytes(raw_bytes)
        results.append({"uid": uid, "from": msg.get("From", ""), "date": msg.get("Date", "")})
    return results


async def _mark_seen(client: aioimaplib.IMAP4_SSL, uid: str) -> None:
    await client.store(uid, "+FLAGS", "\\Seen")


# ── Main IMAP loop ────────────────────────────────────────────────────────────

async def _run_once(settings) -> None:
    """One IMAP session: connect, process unseen messages, then IDLE until error."""
    client = aioimaplib.IMAP4_SSL(host=settings.imap_host, port=settings.imap_port)
    await client.wait_hello_from_server()
    await client.login(settings.imap_user, settings.imap_password.get_secret_value())
    await client.select("INBOX")
    logger.info(
        "imap_listener: connected — %s:%d as %s",
        settings.imap_host, settings.imap_port, settings.imap_user,
    )

    # Drain any messages that arrived while we were offline.
    for m in await _fetch_unseen(client):
        domain = _parse_sender_domain(m["from"])
        if domain:
            _handle_reply(domain, int(time.time() * 1000))
        await _mark_seen(client, m["uid"])

    last_sweep = time.monotonic()
    idle_start = time.monotonic()

    await client.idle_start()
    logger.info("imap_listener: IDLE started")

    while True:
        now = time.monotonic()

        # Periodic timeout sweep.
        if now - last_sweep >= _TIMEOUT_SWEEP_INTERVAL:
            await client.idle_done()
            _sweep_timeouts(settings.ghost_reply_timeout_hours)
            last_sweep = time.monotonic()
            idle_start = time.monotonic()
            await client.idle_start()
            continue

        # Refresh IDLE before the server times it out (~29 min).
        if now - idle_start >= _IDLE_REFRESH_SEC:
            await client.idle_done()
            idle_start = time.monotonic()
            await client.idle_start()
            continue

        # Wait for a server push (EXISTS / RECENT) with a 60-second poll fallback.
        try:
            push = await asyncio.wait_for(client.wait_server_push(), timeout=60.0)
        except asyncio.TimeoutError:
            push = []

        if push and any("EXISTS" in str(p) or "RECENT" in str(p) for p in push):
            await client.idle_done()
            received_at_ms = int(time.time() * 1000)
            for m in await _fetch_unseen(client):
                domain = _parse_sender_domain(m["from"])
                if domain:
                    _handle_reply(domain, received_at_ms)
                await _mark_seen(client, m["uid"])
            idle_start = time.monotonic()
            await client.idle_start()


async def run() -> None:
    settings = get_settings()

    if not settings.imap_enabled:
        logger.warning(
            "imap_listener: IMAP_ENABLED=False — listener will not start. "
            "Ghost Shopper campaigns will stay suspended at WAIT_REPLY until "
            "IMAP_ENABLED is set and the listener is restarted."
        )
        return

    if not settings.imap_password:
        logger.error("imap_listener: IMAP_PASSWORD is not set — cannot connect")
        return

    logger.info("imap_listener: starting (host=%s timeout_hours=%d)",
                settings.imap_host, settings.ghost_reply_timeout_hours)

    while True:
        try:
            await _run_once(settings)
        except Exception as exc:
            logger.error(
                "imap_listener: session error — %s. Reconnecting in %ds.",
                exc, _RECONNECT_DELAY_SEC,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SEC)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main() or 0)
