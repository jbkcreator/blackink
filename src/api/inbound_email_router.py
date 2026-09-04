"""Inbound email webhook — receives forwarded prospect replies via Mailgun.

This router is the HTTP trust boundary only: it verifies the Mailgun HMAC
signature, parses the multipart form, and hands a parsed payload to
src.services.inbound_ingest. All DB work (client resolution, dedup,
attribution, persistence) and the #sales-replies card live in that service,
which owns the blackink_system BYPASSRLS session — CLAUDE.md forbids opening
that session from src/api/.

Security:
  - Mailgun signs every webhook POST with HMAC-SHA256(signing_key, timestamp+token).
    We verify it and reject forged POSTs (3.2.1 trust-boundary rule).
  - We do NOT reject on timestamp age: Mailgun retries non-200 deliveries for
    ~8h, so a legit retry can arrive long after the original timestamp. Replay
    is already a no-op via UNIQUE(message_id) dedup in inbound_ingest, so a
    freshness window would only drop valid retries.
  - MAILGUN_SIGNING_KEY must be set in settings; if absent, all requests 403.

Payload shape (Mailgun HTTP inbound parse, multipart/form-data):
  - recipient       — the Blackink alias (to_alias)
  - from            — original From header (may include display name)
  - subject         — email subject
  - body-plain      — plain-text body
  - timestamp/token/signature — the HMAC preimage + digest
  - message-headers — JSON-encoded list of [name, value] pairs (for In-Reply-To)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status

from config.settings import get_settings
from src.services.inbound_ingest import InboundParsed, ingest_inbound_reply

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


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
    """Verify the Mailgun signature, parse the payload, and delegate ingestion.
    Returns 200 fast (Mailgun retries non-200 for ~8h)."""
    settings = get_settings()

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

    message_headers_json = str(form.get("message-headers", "[]"))
    inbound_message_id = (
        _extract_header(message_headers_json, "message-id")
        or f"<generated-{uuid.uuid4()}@blackink.internal>"
    )

    parsed = InboundParsed(
        to_alias=str(form.get("recipient", "")),
        from_raw=str(form.get("from", "")),
        subject=str(form.get("subject", "")) or None,
        raw_body=str(form.get("body-plain", "")) or None,
        inbound_message_id=inbound_message_id,
        in_reply_to=_extract_header(message_headers_json, "in-reply-to"),
    )

    return await ingest_inbound_reply(parsed)
