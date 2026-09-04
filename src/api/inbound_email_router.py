"""Inbound email webhook — receives forwarded prospect replies via Mailgun.

Flow (ticket 30, spec SPEC-3.1.2-3.1.3.md §Inbound ingestion transport):

  1. Verify Mailgun HMAC-SHA256 signature — reject forged POSTs (3.2.1
     trust-boundary rule).
  2. Resolve client_id from the to_alias ({client_id}@inbound.getblackink.com).
  3. BCC-loop break: if the inbound message_id matches one of our own outbound
     sequence_touch_dispatches.message_id, drop silently (no card, no storage).
  4. Dedup: UNIQUE(message_id) on inbound_messages. If already stored, no-op.
  5. Two-tier attribution (src.services.inbound_attribution):
       a. In-Reply-To → sequence_touch_dispatches.message_id → run → contact
       b. from_address → contacts.email scoped to client
       c. else unattributed
  6. Write inbound_messages row + inbound_reply_received event.
  7. Post #sales-replies card via Slack.
  8. Return 200 fast — Mailgun retries non-200 for ~8h.

Security:
  - Mailgun signs every webhook POST with
    HMAC-SHA256(signing_key, timestamp+token).
  - We verify the signature AND check timestamp freshness (≤5 min) to prevent
    replay attacks. The token is single-use but we don't persist it to Redis
    for Week 1 (timestamp freshness is the primary replay guard).
  - MAILGUN_SIGNING_KEY must be set in settings; if absent, all requests 403.

Payload shape (Mailgun HTTP inbound parse, multipart/form-data):
  - recipient     — the Blackink alias (to_alias)
  - from          — original From header (may include display name)
  - subject       — email subject
  - body-plain    — plain-text body
  - timestamp     — Unix timestamp (str) — part of the signature preimage
  - token         — random token (str) — part of the signature preimage
  - signature     — HMAC-SHA256 hex digest
  - message-headers — JSON-encoded list of [name, value] pairs (for In-Reply-To)

CLAUDE.md invariants observed:
  - blackink_system BYPASSRLS is in src/services/, not src/api/.
    This router imports src.core.database.get_system_db_context via
    inbound_attribution (service layer), not directly.
  - Settings via get_settings(), never os.environ directly.
  - New tenant-bearing table inbound_messages is in TENANT_POLICIES.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from config.settings import get_settings
from src.core.database import get_system_db_context
from src.services.inbound_attribution import attribute, is_bcc_echo, resolve_client_from_alias
from src.services.slack import post as slack_post
from src.services.slack.listeners import _sales_reply_content_blocks  # noqa: WPS450

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

# Maximum age (seconds) of a Mailgun timestamp before we reject the request
# as a potential replay. Mailgun's own replay window is much longer, so
# 300s (5 min) is a tight but practical guard for our side.
_MAX_TIMESTAMP_AGE_SECONDS = 300


def _verify_mailgun_signature(*, signing_key: str, timestamp: str, token: str, signature: str) -> bool:
    """Verify Mailgun's HMAC-SHA256 webhook signature.

    Mailgun computes: HMAC-SHA256(signing_key, timestamp + token) → hex digest.
    We recompute and compare with hmac.compare_digest (constant-time).
    """
    expected = hmac.new(
        key=signing_key.encode("utf-8"),
        msg=(timestamp + token).encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_header(message_headers_json: str, header_name: str) -> Optional[str]:
    """Extract a header value from Mailgun's message-headers JSON field.

    message-headers is a JSON list of [name, value] pairs, e.g.:
    [["Mime-Version", "1.0"], ["In-Reply-To", "<abc@mail.example.com>"], ...]

    Returns the first matching value (case-insensitive on name), or None.
    """
    try:
        headers = json.loads(message_headers_json or "[]")
        for pair in headers:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                if str(pair[0]).lower() == header_name.lower():
                    return str(pair[1])
    except (json.JSONDecodeError, TypeError):
        pass
    return None


@router.post(
    "/inbound-email",
    status_code=status.HTTP_200_OK,
    summary="Mailgun inbound email webhook — reply bridge ingestion",
)
async def inbound_email(request: Request) -> dict:
    """Receive a forwarded prospect reply from Mailgun, attribute it to a
    contact, persist it, and post a #sales-replies card. Returns 200 fast
    (Mailgun retries non-200 for ~8h — respond before doing heavy work)."""
    settings = get_settings()

    # ── 1. Signature verification ──────────────────────────────────────────
    signing_key_secret = settings.mailgun_signing_key
    if signing_key_secret is None:
        logger.error("[inbound_email] MAILGUN_SIGNING_KEY not configured — rejecting all requests")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook not configured")

    signing_key = signing_key_secret.get_secret_value()

    form = await request.form()
    timestamp = str(form.get("timestamp", ""))
    token = str(form.get("token", ""))
    signature = str(form.get("signature", ""))

    if not _verify_mailgun_signature(
        signing_key=signing_key,
        timestamp=timestamp,
        token=token,
        signature=signature,
    ):
        logger.warning("[inbound_email] signature verification failed — rejecting")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    # Timestamp freshness — prevent replay within the 5-min window.
    try:
        ts_int = int(timestamp)
        age = abs(datetime.now(timezone.utc).timestamp() - ts_int)
        if age > _MAX_TIMESTAMP_AGE_SECONDS:
            logger.warning("[inbound_email] timestamp too old (age=%.0fs) — rejecting", age)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Timestamp too old")
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid timestamp")

    # ── 2. Parse payload fields ────────────────────────────────────────────
    to_alias = str(form.get("recipient", ""))  # the Blackink alias
    from_raw = str(form.get("from", ""))
    subject = str(form.get("subject", "")) or None
    raw_body = str(form.get("body-plain", "")) or None
    message_headers_json = str(form.get("message-headers", "[]"))

    # RFC Message-ID of the inbound message itself
    inbound_message_id = _extract_header(message_headers_json, "message-id") or f"<generated-{uuid.uuid4()}@blackink.internal>"
    # In-Reply-To — for Tier 1 attribution
    in_reply_to = _extract_header(message_headers_json, "in-reply-to")

    # Normalize from_address — strip display name if present
    from_address = from_raw.strip()
    if "<" in from_address and from_address.endswith(">"):
        from_address = from_address[from_address.rfind("<") + 1 : -1].strip()
    from_address = from_address or from_raw

    received_at = datetime.now(timezone.utc)

    logger.info(
        "[inbound_email] received from=%s alias=%s in_reply_to=%s",
        from_address,
        to_alias,
        in_reply_to and in_reply_to[:30],
    )

    with get_system_db_context() as session:
        # ── 3. Resolve client from alias ───────────────────────────────────
        client_id = resolve_client_from_alias(session, to_alias)
        if client_id is None:
            logger.warning("[inbound_email] unknown alias=%s — discarding", to_alias)
            return {"status": "discarded", "reason": "unknown_alias"}

        # ── 4. BCC-loop break ──────────────────────────────────────────────
        if is_bcc_echo(session, inbound_message_id):
            logger.info("[inbound_email] BCC echo detected message_id=%s — dropping", inbound_message_id[:40])
            return {"status": "discarded", "reason": "bcc_echo"}

        # ── 5. Dedup by message_id (UNIQUE constraint) ─────────────────────
        existing = session.execute(
            text("SELECT id FROM inbound_messages WHERE message_id = :mid LIMIT 1"),
            {"mid": inbound_message_id},
        ).first()
        if existing is not None:
            logger.info("[inbound_email] duplicate message_id — already stored, no-op")
            return {"status": "discarded", "reason": "duplicate"}

        # ── 6. Attribute ───────────────────────────────────────────────────
        result = attribute(
            session,
            client_id=client_id,
            in_reply_to=in_reply_to,
            from_address=from_address,
        )

        # ── 7. Write inbound_messages ──────────────────────────────────────
        inbound_id = str(uuid.uuid4())
        try:
            session.execute(
                text(
                    """
                    INSERT INTO inbound_messages
                        (id, message_id, client_id, contact_id, run_id,
                         from_address, to_alias, in_reply_to, subject, raw_body,
                         attribution_status, received_at)
                    VALUES
                        (:id, :message_id, :client_id, :contact_id, :run_id,
                         :from_address, :to_alias, :in_reply_to, :subject, :raw_body,
                         :attribution_status, :received_at)
                    """
                ),
                {
                    "id": inbound_id,
                    "message_id": inbound_message_id,
                    "client_id": result.client_id,
                    "contact_id": result.contact_id,
                    "run_id": result.run_id,
                    "from_address": from_address,
                    "to_alias": to_alias,
                    "in_reply_to": in_reply_to,
                    "subject": subject,
                    "raw_body": raw_body,
                    "attribution_status": result.attribution_status,
                    "received_at": received_at,
                },
            )
        except Exception:
            # UNIQUE violation race (concurrent delivery) — already handled by dedup above,
            # but guard the race between check and insert.
            logger.warning("[inbound_email] INSERT conflict on message_id — concurrent duplicate")
            return {"status": "discarded", "reason": "duplicate_race"}

        # ── 8. Write inbound_reply_received event ──────────────────────────
        session.execute(
            text(
                "INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
                "VALUES (:client_id, 'inbound_reply_received', 'inbound_message', :entity_id, 'mailgun_inbound', :payload)"
            ),
            {
                "client_id": result.client_id,
                "entity_id": inbound_id,
                "payload": json.dumps({
                    "inbound_id": inbound_id,
                    "message_id": inbound_message_id,
                    "from_address": from_address,
                    "attribution_status": result.attribution_status,
                    "contact_id": result.contact_id,
                    "run_id": result.run_id,
                }),
            },
        )

        session.commit()

    # ── 9. Post #sales-replies card ────────────────────────────────────────
    card_text = (
        f"💬 Reply from {from_address}"
        + (f" · {result.contact_name}" if result.contact_name else "")
        + (f" ({result.firm_name})" if result.firm_name else "")
        + f" [{result.attribution_status}]"
    )
    blocks = _sales_reply_content_blocks(
        from_address=from_address,
        contact_name=result.contact_name,
        firm_name=result.firm_name,
        run_id=result.run_id,
        touch_step=result.touch_step,
        attribution_status=result.attribution_status,
        subject=subject,
        raw_body=raw_body,
        contact_id=result.contact_id,
        client_id=result.client_id,
        inbound_id=inbound_id,
    )
    await slack_post.post_notice(channel_key="replies", text=card_text, blocks=blocks)

    logger.info(
        "[inbound_email] stored inbound_id=%s attribution=%s",
        inbound_id[:8],
        result.attribution_status,
    )
    return {"status": "ok", "inbound_id": inbound_id}
